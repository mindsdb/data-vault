import ipaddress
import socket
from typing import List
from urllib.parse import urlparse

import pandas as pd
from mindsdb.integrations.libs.response import HandlerStatusResponse
from mindsdb.utilities.config import config
from mindsdb.utilities.security import validate_urls
from .urlcrawl_helpers import get_all_websites

from mindsdb.integrations.libs.api_handler import APIResource, APIHandler
from mindsdb.integrations.utilities.sql_utils import FilterCondition, FilterOperator


def _validate_no_private_ip(url: str) -> None:
    """
    Validates that the given URL does not resolve to a loopback, private,
    link-local, multicast, or reserved IP address to prevent SSRF.
    """
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError(f"Invalid URL provided: {url}")

    try:
        addr_info = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise ValueError(f"Could not resolve hostname for URL '{url}': {e}")

    for info in addr_info:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            if (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_reserved
                or ip_obj.is_multicast
                or ip_obj.is_unspecified
            ):
                raise ValueError(
                    f"Access to private, loopback, or reserved IP addresses is forbidden: {url} ({ip_str})"
                )
        except ValueError as ve:
            if "forbidden" in str(ve):
                raise
            continue


class CrawlerTable(APIResource):
    def list(self, conditions: List[FilterCondition] = None, limit: int = None, **kwargs) -> pd.DataFrame:
        """
        Selects data from the provided websites

        Returns:
            dataframe: Dataframe containing the crawled data

        Raises:
            NotImplementedError: If the query is not supported
        """
        urls = []
        crawl_depth = None
        per_url_limit = None
        headers = {}
        for condition in conditions:
            if condition.column == "url":
                if condition.op == FilterOperator.IN:
                    urls = condition.value
                elif condition.op == FilterOperator.EQUAL:
                    urls = [condition.value]
                condition.applied = True
            if condition.column == "crawl_depth" and condition.op == FilterOperator.EQUAL:
                crawl_depth = condition.value
                condition.applied = True
            if condition.column == "per_url_limit" and condition.op == FilterOperator.EQUAL:
                per_url_limit = condition.value
                condition.applied = True
            if condition.column.lower() == "user_agent" and condition.op == FilterOperator.EQUAL:
                headers["User-Agent"] = condition.value
                condition.applied = True

        if len(urls) == 0:
            raise NotImplementedError(
                'You must specify what url you want to crawl, for example: SELECT * FROM web.crawler WHERE url = "someurl"'
            )

        for url in urls:
            _validate_no_private_ip(url)

        allowed_urls = config.get("web_crawling_allowed_sites", [])
        if allowed_urls and not validate_urls(urls, allowed_urls):
            raise ValueError(
                f"The provided URL is not allowed for web crawling. Please use any of {', '.join(allowed_urls)}."
            )

        if limit is None and per_url_limit is None and crawl_depth is None:
            per_url_limit = 1
        if per_url_limit is not None:
            # crawl every url separately
            results = []
            for url in urls:
                results.append(get_all_websites([url], per_url_limit, crawl_depth=crawl_depth, headers=headers))
            result = pd.concat(results)
        else:
            result = get_all_websites(urls, limit, crawl_depth=crawl_depth, headers=headers)

        if limit is not None and len(result) > limit:
            result = result[:limit]

        return result

    def get_columns(self):
        """
        Returns the columns of the crawler table
        """
        return ["url", "text_content", "error"]


class WebHandler(APIHandler):
    """
    Web handler, handling crawling content from websites.
    """

    def __init__(self, name=None, **kwargs):
        super().__init__(name)
        crawler = CrawlerTable(self)
        self._register_table("crawler", crawler)

    def check_connection(self) -> HandlerStatusResponse:
        """
        Checks the connection to the web handler
        @TODO: Implement a better check for the connection

        Returns:
            HandlerStatusResponse: Response containing the status of the connection. Hardcoded to True for now.
        """
        response = HandlerStatusResponse(True)
        return response

