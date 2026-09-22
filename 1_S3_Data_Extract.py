import deepdiff
import boto3
import hashlib
import json
import requests
import operator

bucket_name = 'cct-ds-code-challenge-input-data'
keys = 'ds_code_challenge_creds.json'
url = "https://cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com/"
region = 'af-south-1'

class grabKeys: 
    def __init__(self):
        pass
    def get_keys(self, url, keyFile):
        self.url = url
        self.keyFile = keyFile
        
        response = requests.get((self.url+self.keyFile))
        if response.status_code == 200:
            data = response.json()
            access_key = data['s3']['access_key']
            secret_key = data['s3']['secret_key']
            return access_key, secret_key
        else:
            print(f"Request failed with status code {response.status_code}")
            return None, None
        
class S3Select:
    def __init__(self, url, region, keyFile):
        self.s3 = boto3.client(
            's3',
            aws_access_key_id=grabKeys().get_keys(url, keyFile)[0],
            aws_secret_access_key=grabKeys().get_keys(url, keyFile)[1],
            region_name=region
        )

    def select_data(self, bucket, key, expression):
        resp = self.s3.select_object_content(
            Bucket=bucket,
            Key=key,
            ExpressionType='SQL',
            Expression=expression,
            InputSerialization={
                'JSON': {'Type': 'DOCUMENT'},
                'CompressionType': 'NONE' 
            },
            OutputSerialization={
                'JSON': {'RecordDelimiter': '\n'}
            }
        )
        return resp

class printRecords:
    def __init__(self, response):
        self.response = response

    def print_data(self):
        for event in self.response['Payload']:
            if 'Records' in event:
                records = event['Records']['Payload'].decode('utf-8')
                print(records, end='')
class jsonConv:
    def __init__(self, response):
        self.response = response

    def convert_to_json(self):
      buf = bytearray()
      for event in self.response['Payload']:
          if 'Records' in event:
              buf += event['Records']['Payload']     # accumulate across every event
      lines = [l for l in buf.decode('utf-8').split('\n') if l.strip()]
      records = [json.loads(l) for l in lines]
      return records   
  
def canon(props):
    shared = {k: props[k] for k in ("index", "centroid_lat", "centroid_lon")}
    blob = json.dumps(shared, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()     

def deepValidation(json1, json2):
    newSchema = []
    
    sorted_json1 = sorted(json1, key=lambda x: x['index'])
    sorted_json2 = sorted(json2, key=lambda x: x['index'])

    for obj1, obj2 in zip(sorted_json1, sorted_json2):
        obj = {
            'index1': obj1['index'],
            'index2': obj2['index'],
            'sim': (1 - (deepdiff.DeepDiff(obj1, obj2, get_deep_distance=True)).get('deep_distance', 0))*100
        }
        newSchema.append(obj)
    with open('validation_results.json', 'w') as f:
        json.dump(newSchema, f, indent=4)

def cheapValidation(json1, json2):
    hashes1 = sorted(canon(record) for record in json1)
    hashes2 = sorted(canon(record) for record in json2)
    return hashes1 == hashes2

resQuery = "SELECT s.properties.index, s.properties.centroid_lat, s.properties.centroid_lon FROM S3Object[*].features[*] s where s.properties.resolution = 8"
resFile = 'city-hex-polygons-8-10.geojson'
validationQuery = "SELECT s.properties.index, s.properties.centroid_lat, s.properties.centroid_lon FROM S3Object[*].features[*] s"
validationFile = 'city-hex-polygons-8.geojson'

queriedProps = S3Select(url, region, keys).select_data(bucket_name, resFile, resQuery)
validationProps = S3Select(url, region, keys).select_data(bucket_name, validationFile, validationQuery)
queriedJson = jsonConv(queriedProps).convert_to_json()
validationJson = jsonConv(validationProps).convert_to_json()

if cheapValidation(queriedJson, validationJson):
    with open('cheap_validation_results.json', 'w') as f:
        json.dump("The two JSON objects are equivalent.", f)
        json.dump(queriedJson, f)
else:
    with open('cheap_validation_results.json', 'w') as f:
        json.dump("The two JSON objects are not equivalent.\n", f)
        json.dump(queriedJson, f)
        json.dump(validationJson, f)
                
deepValidation(queriedJson, validationJson)
