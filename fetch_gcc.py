awesome. Now when the slot is full it shows as error. Instead display as slot full. also when the slot is available display the slots available in the payment screen of jackslotix. 

I tried paying and the payment has been deducted however there is no booking confirmation in the website nor in the whatsapp. Note: usually when i make payment in the GCC website. after payment is succesful the website shows a dialogue box of succesful payment and we have to hit OK.import requests

url = "https://gccservices.in/muthalvarpadaippagam/book"
headers = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
}

try:
    html = requests.get(url, headers=headers, verify=False, timeout=10).text
    with open("gcc.html", "w") as f:
        f.write(html)
    print("Saved gcc.html")
except Exception as e:
    print(f"Failed: {e}")
