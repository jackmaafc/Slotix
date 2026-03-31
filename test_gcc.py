import requests

GCC_HEADERS = {
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://gccservices.in",
    "referer": "https://gccservices.in/muthalvarpadaippagam/book",
    "x-requested-with": "XMLHttpRequest",
    "user-agent": "Mozilla/5.0"
}

reserve_data = {
    "catId": 4,
    "buildId": 2,
    "subId": 8,
    "noOfPeople": 2,
    "fromDate": "2026-04-01",
    "toDate": "2026-04-01",
    "userName": "Test",
    "userMobile": "9999999999",
    "slots[]": 1
}

resp = requests.post(
    "https://gccservices.in/muthalvarpadaippagam/book/api/saveInTemp",
    data=reserve_data,
    headers=GCC_HEADERS
)
print(resp.status_code)
print(resp.json())
