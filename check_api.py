import getpass
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if len(sys.argv) != 2 or not sys.argv[1].startswith("/api/"):
    raise SystemExit("사용법: python check_api.py /api/경로")
token = getpass.getpass("앱 토큰: ")
request = Request(
    "http://127.0.0.1:8001" + sys.argv[1],
    headers={"Authorization": "Bearer " + token},
)
try:
    with urlopen(request, timeout=60) as response:
        print("HTTP", response.status)
        print(response.read().decode())
except HTTPError as error:
    print("HTTP", error.code)
    print(error.read().decode())
except URLError as error:
    print("연결 실패:", error.reason)