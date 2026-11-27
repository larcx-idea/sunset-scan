"""Pull page insights and shop orders for client dashboards."""
import requests
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.adaccount import AdAccount

GRAPH = "https://graph.facebook.com/v20.0"


def page_insights(page_id: str, token: str) -> dict:
    url = f"{GRAPH}/{page_id}/insights"
    return requests.get(url, params={"metric": "page_impressions", "access_token": token}, timeout=30).json()


def shop_orders(page_id: str, token: str) -> dict:
    url = f"{GRAPH}/{page_id}/commerce_orders"
    return requests.get(url, params={"access_token": token}, timeout=30).json()


def ad_spend(account_id: str, token: str):
    FacebookAdsApi.init(access_token=token, api_version="v24.0")
    return AdAccount(f"act_{account_id}").get_insights(fields=["spend"])
