import boto3
import json
import requests

url = "https://cct-ds-code-challenge-input-data.s3.af-south-1.amazonaws.com/ds_code_challenge_creds.json"
response = requests.get(url)

if response.status_code == 200:
    data = response.json()
    access_key = (data['s3']['access_key'])
    secret_key =(data['s3']['secret_key'])
else:
    print(f"Request failed with status code {response.status_code}")

s3 = boto3.client(
    's3',
    aws_access_key_id=access_key,
    aws_secret_access_key=secret_key,
    region_name='af-south-1'
)

resp = s3.select_object_content(
    Bucket='cct-ds-code-challenge-input-data',
    Key='city-hex-polygons-8.geojson',
    ExpressionType='SQL',
    Expression="SELECT s.properties, s.geometry FROM S3Object[*].features[*] s",
    InputSerialization={
        'JSON': {'Type': 'DOCUMENT'},
        'CompressionType': 'NONE' 
    },
    OutputSerialization={
    'JSON': {'RecordDelimiter': '\n'}
    }
)

print(s3.get_object(Bucket="cct-ds-code-challenge-input-data", Key='city-hex-polygons-8-10.geojson',
                   Range='bytes=0-2000')['Body'].read())