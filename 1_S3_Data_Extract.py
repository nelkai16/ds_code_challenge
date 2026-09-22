import boto3
import json
import requests

class grabKeys: 
    def get_keys(self, url):
        self.url = url
        response = requests.get(self.url)
        if response.status_code == 200:
            data = response.json()
            access_key = data['s3']['access_key']
            secret_key = data['s3']['secret_key']
            return access_key, secret_key
        else:
            print(f"Request failed with status code {response.status_code}")
            return None, None
        
class S3Select:
    def __init__(self, url, region):
        self.s3 = boto3.client(
            's3',
            aws_access_key_id=grabKeys().get_keys(url)[0],
            aws_secret_access_key=grabKeys().get_keys(url)[1],
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

bucket_name = 'cct-ds-code-challenge-input-data'
file = 'city-hex-polygons-8-10.geojson'
query = "SELECT s.properties, s.geometry FROM S3Object[*].features[*] s where s.properties.resolution = 8"
url = "https://cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com/ds_code_challenge_creds.json"
region = 'af-south-1'

printRecords(S3Select(url, region).select_data(bucket_name, file, query)).print_data()