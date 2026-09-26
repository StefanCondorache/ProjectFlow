import requests


class Gateway:
    def charge(self, amount):
        resp = requests.post("https://pay.example/charge", json={"amount": amount})
        return resp.json()["receipt"]
