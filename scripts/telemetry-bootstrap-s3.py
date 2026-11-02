"""Create the local Langfuse S3 bucket; reads secrets without printing them."""
import base64
import hashlib
import hmac
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from dotenv import dotenv_values


def signed_request(secret, payload=b'', query=''):
    now = datetime.now(UTC)
    stamp = now.strftime('%Y%m%dT%H%M%SZ')
    day = now.strftime('%Y%m%d')
    digest = hashlib.sha256(payload).hexdigest()
    host = 'localhost:9092'
    headers = f'host:{host}\nx-amz-content-sha256:{digest}\nx-amz-date:{stamp}\n'
    signed = 'host;x-amz-content-sha256;x-amz-date'
    canonical = f'PUT\n/langfuse\n{query}\n{headers}\n{signed}\n{digest}'
    scope = f'{day}/auto/s3/aws4_request'
    to_sign = f'AWS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    def sign(key, message):
        return hmac.new(key, message.encode(), hashlib.sha256).digest()
    key = sign(sign(sign(sign(('AWS4' + secret).encode(), day), 'auto'), 's3'), 'aws4_request')
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f'AWS4-HMAC-SHA256 Credential=langfuse/{scope}, SignedHeaders={signed}, Signature={signature}'
    suffix = '?' + query if query else ''
    return urllib.request.Request(f'http://{host}/langfuse{suffix}', data=payload, method='PUT', headers={
        'Authorization': auth, 'x-amz-date': stamp, 'x-amz-content-sha256': digest,
        'Content-Type': 'application/xml',
        'Content-MD5': base64.b64encode(hashlib.md5(payload, usedforsecurity=False).digest()).decode()})


def main():
    env = dotenv_values(Path(__file__).resolve().parents[1] / '.env.local')
    secret = env['TELEMETRY_S3_PASSWORD']
    request = signed_request(secret)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            print(f'Local Langfuse bucket ready (HTTP {response.status})')
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        if exc.code == 409 and 'BucketAlreadyOwnedByYou' in body:
            print('Local Langfuse bucket already exists')
        else:
            raise RuntimeError(f'Local bucket creation failed: HTTP {exc.code}') from None
    lifecycle = b'''<LifecycleConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/">
<Rule><ID>telemetry-30-days</ID><Filter><Prefix></Prefix></Filter><Status>Enabled</Status>
<Expiration><Days>30</Days></Expiration>
<AbortIncompleteMultipartUpload><DaysAfterInitiation>7</DaysAfterInitiation></AbortIncompleteMultipartUpload>
</Rule></LifecycleConfiguration>'''
    with urllib.request.urlopen(signed_request(secret, lifecycle, 'lifecycle='), timeout=15) as response:
        print(f'Local telemetry objects: expiration 30 days (HTTP {response.status})')


if __name__ == '__main__':
    main()
