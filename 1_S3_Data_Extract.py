from urllib import response

import boto3
import json
import jsondiff
import requests

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

resQuery = "SELECT s.properties.index, s.properties.centroid_lat, s.properties.centroid_lon FROM S3Object[*].features[*] s where s.properties.resolution = 8"
resFile = 'city-hex-polygons-8-10.geojson'
validationQuery = "SELECT s.properties.index, s.properties.centroid_lat, s.properties.centroid_lon FROM S3Object[*].features[*] s"
validationFile = 'city-hex-polygons-8.geojson'

queriedProps = S3Select(url, region, keys).select_data(bucket_name, resFile, resQuery)
validationProps = S3Select(url, region, keys).select_data(bucket_name, validationFile, validationQuery)
queriedJson = jsonConv(queriedProps).convert_to_json()
validationJson = jsonConv(validationProps).convert_to_json()

delta = jsondiff.diff(validationJson, queriedJson, syntax='symmetric', dump=True)
print(json.dumps(delta, indent=2))

#Looking at this wrong. Files = seperate from additional validation. Validation schema is new file composed of score
#   from validation. Reading the validation here as two parts. 1st validation, using the 8 file. This is plainly stated so
#   will maintain the json diff as a "rough" validation on a raw compare. Second validation, interpreting as that due to
#   wording of additional. Going to include the geo data now so that there is more "source" to validate. Will output this  
#   to a new .json file composed of the index as the key, and validation score