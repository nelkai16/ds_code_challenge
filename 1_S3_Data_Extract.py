"""Section 1 - extract the H3 resolution 8 hexagon set and validate the extract.

Extracts the resolution 8 slice of city-hex-polygons-8-10.geojson with S3 SELECT, writes the dataset out,
then validates it two ways: it is scored against the conformance contract in schema.yml, and it is
cross-checked against the provided city-hex-polygons-8.geojson, which is the correctness oracle for the
fields the two files share.

Outputs, all written to the repository root:
  r8_hexagons.geojson      the extracted dataset, features ordered by index
  validation_report.json   conformance score, cross-check result, per check tallies, timings, failures
  validation_results.json  per record similarity to the reference file, written by the cross-check
  validation.log           full run log: one line per record, run summary, timings and byte counts
"""

#Personally would rather use DB validation based on triggers and stored procs, would treat this as a daily load/once off load
#DB triggers+validation should be hardened enough to reject bad data
#All actions should be piped to a log table
#Would rather keep the integration on DB side to avoid having to manage multiple moving parts in the data pipeline
#As well as having logs easily accessible in the DB for auditing and debugging purposes
#Python would be good for integration layer or to push to DB
#Though that would depend on if this a once off load or expected daily interface

import hashlib
import json
import logging
import os
import string
import sys
import time
from typing import Any

import boto3
import deepdiff
import requests
import yaml

bucket_name = "cct-ds-code-challenge-input-data"
keys = "ds_code_challenge_creds.json"
url = "https://cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com/"
region = "af-south-1"

resQuery = "SELECT s.properties.* FROM S3Object[*].features[*] s where s.properties.resolution = 8"
resFile = "city-hex-polygons-8-10.geojson"
validationQuery = "SELECT s.properties.index, s.properties.centroid_lat, s.properties.centroid_lon FROM S3Object[*].features[*] s"
validationFile = "city-hex-polygons-8.geojson"

SCHEMA = "schema.yml"
LOG = "validation.log"
REPORT = "validation_report.json"
EXTRACT = "r8_hexagons.geojson"
DETAIL = "validation_results.json"

# The files above are read and written next to the script, not in the working directory, so a run
# from anywhere finds schema.yml and leaves its outputs in the repository.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def local(name):
    return os.path.join(BASE_DIR, name)

RESOLUTION_BITS = 52  # H3 packs the resolution into the four highest bits of the 64 bit index
SHARED_FIELDS = ("index", "centroid_lat", "centroid_lon")  # the fields the extract and the reference share

logger = logging.getLogger("validation")

# Populated by main() so the report carries the timings, byte counts and cross-check result of the run.
RUN: dict[str, Any] = {"timings": {}, "bytes": {}, "cross_check": {}}
CONTRACT = {}


class grabKeys:
    def __init__(self):
        pass

    def get_keys(self, url, keyFile):
        self.url = url
        self.keyFile = keyFile

        response = requests.get((self.url + self.keyFile))
        if response.status_code == 200:
            data = response.json()
            access_key = data["s3"]["access_key"]
            secret_key = data["s3"]["secret_key"]
            return access_key, secret_key
        else:
            logger.warning("Request failed with status code %s", response.status_code)
            return None, None


class S3Select:
    def __init__(self, url, region, keyFile):
        self.s3 = boto3.client(
            "s3",
            aws_access_key_id=grabKeys().get_keys(url, keyFile)[0],
            aws_secret_access_key=grabKeys().get_keys(url, keyFile)[1],
            region_name=region,
        )

    def select_data(self, bucket, key, expression):
        resp = self.s3.select_object_content(
            Bucket=bucket,
            Key=key,
            ExpressionType="SQL",
            Expression=expression,
            InputSerialization={
                "JSON": {"Type": "DOCUMENT"},
                "CompressionType": "NONE",
            },
            OutputSerialization={"JSON": {"RecordDelimiter": "\n"}},
        )
        return resp


class jsonConv:
    """Turns an S3 SELECT payload stream into records, and keeps the bytes it scanned."""

    def __init__(self, response):
        self.response = response
        self.stats = {}

    def convert_to_json(self):
        buf = bytearray()
        for event in self.response["Payload"]:
            if "Records" in event:
                buf += event["Records"]["Payload"]  # accumulate across every event
            elif "Stats" in event:
                self.stats = event["Stats"]["Details"]
        lines = [l for l in buf.decode("utf-8").split("\n") if l.strip()]
        return [json.loads(l) for l in lines]


def canon(props):
    shared = {k: props[k] for k in SHARED_FIELDS}
    blob = json.dumps(shared, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def deepValidation(json1, json2):
    """Pair the two sides by sorted index and record how similar each pair is.

    Returns the detail rows; it also writes them to validation_results.json as the record of the
    cross-check.
    """
    newSchema = []

    sorted_json1 = sorted(json1, key=lambda x: x["index"])
    sorted_json2 = sorted(json2, key=lambda x: x["index"])

    for obj1, obj2 in zip(sorted_json1, sorted_json2):
        obj = {
            "index1": obj1["index"],
            "index2": obj2["index"],
            "sim": (
                1
                - (deepdiff.DeepDiff(obj1, obj2, get_deep_distance=True)).get(
                    "deep_distance", 0
                )
            )
            * 100,
        }
        newSchema.append(obj)
    with open(local(DETAIL), "w", encoding="utf-8", newline="\n") as detail_file:
        json.dump(newSchema, detail_file, indent=4)
    return newSchema


def cheapValidation(json1, json2):
    hashes1 = sorted(canon(record) for record in json1)
    hashes2 = sorted(canon(record) for record in json2)
    return hashes1 == hashes2


def project(record):
    """Reduce a record to the fields the extract and the reference file share.

    The reference has no resolution attribute and the extract carries fields it does not, so both sides are
    reduced to the shared set before they are compared. Comparing them whole would score the extract down for
    fields the reference was never expected to have.
    """
    return {field: record[field] for field in SHARED_FIELDS if field in record}


def crossCheck(records, reference):
    """Validate the extract against the provided reference file, README line 104.

    Returns a summary of the comparison; the per record similarity detail is written to disk by
    deepValidation, and only once both sides are known to cover the same indices, because pairs are formed
    by sorted position and zip would silently truncate a short side and still look perfect.
    """
    summary = {
        "reference": validationFile,
        "records_extracted": len(records),
        "records_reference": len(reference),
        "index_sets_equal": False,
        "identical": False,
    }

    if not records or not reference:
        summary["reason"] = "one side is empty, there is nothing to compare"
        logger.error("cross check skipped: %s", summary["reason"])
        return summary

    projected = [project(record) for record in records]
    expected = [project(record) for record in reference]

    if any(field not in record for record in projected for field in SHARED_FIELDS):
        summary["reason"] = "a record is missing one of the shared fields"
        logger.error("cross check skipped: %s", summary["reason"])
        return summary

    summary["index_sets_equal"] = {r["index"] for r in projected} == {r["index"] for r in expected}
    if not summary["index_sets_equal"]:
        summary["reason"] = "the two sides do not cover the same indices"
        logger.error("cross check failed: %s", summary["reason"])
        return summary

    detail = deepValidation(projected, expected)
    similarity = [row["sim"] for row in detail]
    summary["records_compared"] = len(detail)
    summary["similarity_min"] = round(min(similarity), 6)
    summary["similarity_mean"] = round(sum(similarity) / len(similarity), 6)
    summary["similarity_max"] = round(max(similarity), 6)
    summary["records_below_100"] = sum(1 for value in similarity if value < 100)
    summary["identical"] = (
        cheapValidation(projected, expected) and summary["records_below_100"] == 0
    )
    return summary


def check_required_keys(record):
    """index, centroid_lat and centroid_lon must be present on the record."""
    missing = sorted(set(SHARED_FIELDS) - set(record))
    if missing:
        return False, f"missing keys: {missing}"
    return True, ""


def check_index_format(record):
    """The index must be 15 lowercase hex characters carrying the resolution 8 prefix."""
    idx = record.get("index", "")
    if not isinstance(idx, str):
        return False, f"index is {type(idx).__name__}, expected str"
    if len(idx) != 15:
        return False, f"index is {len(idx)} characters, expected 15"
    if idx != idx.lower() or any(char not in string.hexdigits.lower() for char in idx):
        return False, "index is not lowercase hex"
    if not idx.startswith("8"):
        return False, "index does not carry the resolution 8 prefix"
    return True, ""


def check_resolution_is_8(record):
    """The resolution attribute must be the integer 8 and agree with the bits in the index."""
    ok, reason = check_index_format(record)
    if not ok:
        return False, f"resolution cannot be derived, {reason}"
    if record.get("resolution") != 8:
        return False, f"resolution attribute is {record.get('resolution')!r}, expected 8"
    derived = (int(record["index"], 16) >> RESOLUTION_BITS) & 0xF
    if derived != 8:
        return False, f"resolution derived from index is {derived}, expected 8"
    return True, ""


# One implementation per check id the schema may declare. The schema drives which checks run, so the
# contract itself is never duplicated here.
CHECKS = {
    "required_keys": check_required_keys,
    "index_format": check_index_format,
    "resolution_is_8": check_resolution_is_8,
}


def gate_count_positive(records):
    if not records:
        return "the query returned no records"
    return ""


def gate_index_unique(records):
    indices = {record.get("index") for record in records}
    if len(indices) != len(records):
        return f"{len(records) - len(indices)} duplicate indices present"
    return ""


GATES = {
    "count_positive": gate_count_positive,
    "index_unique": gate_index_unique,
}


def load_schema(path):
    """Read the conformance contract and check it against what is actually implemented."""
    with open(path, encoding="utf-8") as schema_file:
        contract = yaml.safe_load(schema_file)

    unknown = sorted({check["id"] for check in contract["record_checks"]} - set(CHECKS))
    if unknown:
        raise ValueError(f"{path} declares checks with no implementation: {unknown}")

    total_weight = sum(check["weight"] for check in contract["record_checks"])
    if round(total_weight, 6) != 1.0:
        raise ValueError(f"{path} weights sum to {total_weight}, expected 1.0")

    for gate in contract["dataset_gates"]:
        if gate["id"] not in GATES:
            raise ValueError(f"{path} declares a gate with no implementation: {gate['id']}")

    return contract


def evaluate_gates(records, contract):
    """Dataset level gates. A failure stops the run rather than lowering the score."""
    return [
        f"{gate['id']}: {reason}"
        for gate in contract["dataset_gates"]
        if (reason := GATES[gate["id"]](records))
    ]


def evaluate_checks(records, contract):
    """Evaluate every declared check against every record as extracted.

    Returns the per check tallies, the failures keyed by index, and how many records failed at least one
    check.
    """
    tally = {
        check["id"]: {"passing": 0, "failing": 0} for check in contract["record_checks"]
    }
    failure = {}
    records_failing = 0

    for record in records:
        idx = record.get("index", "Unknown")
        failed_here = False
        for check in contract["record_checks"]:
            ok, reason = CHECKS[check["id"]](record)
            if ok:
                tally[check["id"]]["passing"] += 1
                continue
            tally[check["id"]]["failing"] += 1
            failed_here = True
            failure.setdefault(idx, []).append({"check": check["id"], "reason": reason})
            logger.warning("record %s failed %s: %s", idx, check["id"], reason)
        if failed_here:
            records_failing += 1
        else:
            logger.debug("record %s passed every check: %s", idx, record)

    return tally, failure, records_failing


def score_records(tally, contract, total):
    """Weighted pass rate across the declared checks, as a percentage."""
    if not total:
        return 0.0
    weighted = sum(
        check["weight"] * tally[check["id"]]["passing"] / total
        for check in contract["record_checks"]
    )
    return round(100 * weighted, 2)


def write_extract(records, path):
    """Write the extracted dataset as a GeoJSON FeatureCollection, ordered by index.

    The query selects properties only, so geometry is carried as null. Section 2 joins on the hexagon
    index, so geometry is not needed for that join.
    """
    collection = {
        "type": "FeatureCollection",
        "features": [
            {"type": "Feature", "properties": record, "geometry": None}
            for record in sorted(records, key=lambda record: record.get("index", ""))
        ],
    }
    # newline="\n" so the committed extract is byte for byte what a run on Windows writes as well.
    with open(path, "w", encoding="utf-8", newline="\n") as extract_file:
        json.dump(collection, extract_file, indent=4)
    return path


def writeReport(failure, tally, score, status):
    """Write the conformance report. Timings, byte counts and the cross-check result come from RUN."""
    weights = {check["id"]: check["weight"] for check in CONTRACT["record_checks"]}
    report = {
        "schema": SCHEMA,
        "dataset": CONTRACT["dataset"],
        "threshold": CONTRACT["threshold"],
        "records_evaluated": RUN["records_evaluated"],
        "records_passing": RUN["records_passing"],
        "records_failing": RUN["records_failing"],
        "score": score,
        "status": status,
        "checks": {
            check_id: {
                "weight": weights[check_id],
                "passing": counts["passing"],
                "failing": counts["failing"],
            }
            for check_id, counts in tally.items()
        },
        "cross_check": RUN["cross_check"],
        "timings_seconds": RUN["timings"],
        "bytes": RUN["bytes"],
        "failures": failure,
    }
    with open(local(REPORT), "w", encoding="utf-8", newline="\n") as report_file:
        json.dump(report, report_file, indent=4)
    return REPORT


def configure_logging():
    """Log every record to validation.log, and the run summary to the console as well."""
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    # %Z so the timestamps carry the machine's timezone, as section 2's do.
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S %Z")

    log_file = logging.FileHandler(local(LOG), mode="a", encoding="utf-8")
    log_file.setLevel(logging.DEBUG)
    log_file.setFormatter(formatter)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(formatter)

    logger.addHandler(log_file)
    logger.addHandler(console)
    logger.info("%s", "-" * 70)


def main():
    """Extract the resolution 8 set, write it out, and validate it against the contract and the reference."""
    global CONTRACT

    configure_logging()
    started = time.perf_counter()
    CONTRACT = load_schema(local(SCHEMA))
    logger.info("run start: contract %s, threshold %s", SCHEMA, CONTRACT["threshold"])

    tick = time.perf_counter()
    queried = jsonConv(
        S3Select(url, region, keys).select_data(bucket_name, resFile, resQuery)
    )
    queriedJson = queried.convert_to_json()
    RUN["timings"]["extract_source"] = round(time.perf_counter() - tick, 3)
    RUN["bytes"]["source"] = queried.stats
    logger.info(
        "extracted %s records from %s in %ss (%s)",
        len(queriedJson),
        resFile,
        RUN["timings"]["extract_source"],
        queried.stats,
    )

    tick = time.perf_counter()
    reference = jsonConv(
        S3Select(url, region, keys).select_data(bucket_name, validationFile, validationQuery)
    )
    validationJson = reference.convert_to_json()
    RUN["timings"]["extract_reference"] = round(time.perf_counter() - tick, 3)
    RUN["bytes"]["reference"] = reference.stats
    logger.info(
        "fetched %s records from %s in %ss (%s)",
        len(validationJson),
        validationFile,
        RUN["timings"]["extract_reference"],
        reference.stats,
    )

    tick = time.perf_counter()
    gate_errors = evaluate_gates(queriedJson, CONTRACT)
    RUN["timings"]["gates"] = round(time.perf_counter() - tick, 4)
    if gate_errors:
        logger.error("dataset gates failed: %s", "; ".join(gate_errors))
        sys.exit(1)

    tick = time.perf_counter()
    write_extract(queriedJson, local(EXTRACT))
    RUN["timings"]["write_extract"] = round(time.perf_counter() - tick, 3)
    logger.info("wrote %s", EXTRACT)

    tick = time.perf_counter()
    tally, failure, records_failing = evaluate_checks(queriedJson, CONTRACT)
    RUN["timings"]["checks"] = round(time.perf_counter() - tick, 3)
    RUN["records_evaluated"] = len(queriedJson)
    RUN["records_failing"] = records_failing
    RUN["records_passing"] = len(queriedJson) - records_failing

    tick = time.perf_counter()
    RUN["cross_check"] = crossCheck(queriedJson, validationJson)
    RUN["timings"]["cross_check"] = round(time.perf_counter() - tick, 3)
    logger.info("cross check against %s: %s", validationFile, RUN["cross_check"])

    score = score_records(tally, CONTRACT, len(queriedJson))
    status = "PASS" if score >= CONTRACT["threshold"] else "FAIL"
    writeReport(failure, tally, score, status)

    RUN["timings"]["total"] = round(time.perf_counter() - started, 3)
    weights = {check["id"]: check["weight"] for check in CONTRACT["record_checks"]}
    for check_id, counts in tally.items():
        logger.info(
            "check %s (weight %s): %s passing, %s failing",
            check_id,
            weights[check_id],
            counts["passing"],
            counts["failing"],
        )
    logger.info(
        "records evaluated %s, passing %s, failing %s",
        RUN["records_evaluated"],
        RUN["records_passing"],
        RUN["records_failing"],
    )
    logger.info(
        "score %s against threshold %s -> %s (report: %s)",
        score,
        CONTRACT["threshold"],
        status,
        REPORT,
    )
    logger.info("timings in seconds: %s", RUN["timings"])
    logger.info("bytes: %s", RUN["bytes"])

    cross_ok = bool(RUN["cross_check"].get("identical"))
    logger.info(
        "identical to %s: %s | exit %s",
        validationFile,
        cross_ok,
        0 if (status == "PASS" and cross_ok) else 1,
    )

    sys.exit(0 if status == "PASS" and cross_ok else 1)


if __name__ == "__main__":
    main()
