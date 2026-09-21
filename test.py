import boto3
import json

s3 = boto3.client(
    's3',
    aws_access_key_id='',
    aws_secret_access_key='',
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