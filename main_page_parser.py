"""
Original Author: Father1993  
Modified By: lmh17ever

Module for scraping car-related websites using Playwright library.

This module provides functionality to extract detailed information such as car titles, prices,
mileages, configurations, and associated images from specific car listing pages.
It also includes utilities for downloading images and storing extracted data
in structured formats like JSON files.

Classes:
---------
CarParser: Main class responsible for extracting car details and downloading related images.

Functions:
----------
None at top-level; methods are encapsulated within the CarParser class.

Usage Example:
data = parser.parse_car_page("https://dongchedi.com/usedcar/12345")
"""

import json
import os
import time
from shutil import rmtree
import requests
from playwright.sync_api import sync_playwright
from configuration_parser import get_data
from constants import USER_AGENT, SPECIAL_CHARS_TO_NUMBERS


class CarParser:
    """Class for parsing car pages from DongCheDi with progress messages."""

    IMAGE_SELECTOR = '.tw-flex-none.tw-w-100\\/6 img'
    PRICE_SELECTOR = '.jsx-1166026127.tw-text-color-red-500'
    MILEAGE_BLOCK = '.jsx-1166026127.tw-flex-auto.tw-flex.tw-flex-col.tw-justify-center'
    CONFIG_LINK_SELECTOR = '.tw-flex-none.tw-text-color-gray-800'
    SCROLL_BUTTON_SELECTOR = 'button.tw--right-8.head-info_swiper-button__Z2mjF'
    DISABLED_CLASS = 'swiper-button-disabled'

    def __init__(self, queue=None):
        """
        Parameters
        ----------
        queue : Queue, optional
            Queue to send progress messages to (GUI or logger), default is None.
        """
        self.queue = queue

    # ----------------------------
    # Public methods
    # ----------------------------

    def parse_car_page(self, url: str) -> dict:
        """Parse a car page and send progress messages at each step.

        Launches its own browser — used by the desktop GUI, which parses
        one URL at a time and can afford a 5-minute wait for a human to
        deal with anything unexpected (captcha, slow network).
        """

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False)
            context = browser.new_context(user_agent=USER_AGENT)
            page = context.new_page()

            car_data = self.parse_car_page_using(page, url, timeout_ms=300_000)

            context.close()
            browser.close()
            self._log("Parsing completed")

            return car_data

    def parse_car_page_using(self, page, url: str, save_local: bool = True, timeout_ms: int = 300_000) -> dict:
        """Parse a car page on an already-open Playwright page.

        Lets a caller reuse one browser/context across many listings (the
        weekly batch scraper) instead of launching a fresh browser per car.
        ``timeout_ms`` should be much shorter than the GUI default for batch
        runs — a dead or sold listing should be skipped in seconds, not
        block the whole run for five minutes.
        """
        page.goto(url, wait_until='networkidle')
        page.wait_for_selector('.head-info_price-wrap__Y4bxi', timeout=timeout_ms)
        self._log("Page loaded successfully")

        car_title = self._extract_title(page)
        self._log(f"Title extracted: {car_title}")

        price = self._extract_price(page)
        self._log(f"Price extracted: {price}")

        mileage = self._extract_mileage(page)
        self._log(f"Mileage extracted: {mileage}")

        self._scroll_images(page)
        images = self._extract_images(page)
        self._log(f"{len(images)} images extracted")

        configuration_info = self._get_configuration_info(page)
        self._log("Configuration data retrieved")

        car_data = {
            "title": car_title,
            "price": price,
            "mileage": mileage,
            "url": url,
            "configuration_info": configuration_info,
            "images": images
        }

        if save_local:
            car_dir = self._prepare_storage_and_save(car_data)
            self._log(f"Data saved to {car_dir}")
            self.download_images(images, car_dir)

        return car_data

    def download_images(self, image_urls: list, save_dir: str):
        """Download images and send messages for each download."""
        headers = {
            'User-Agent': USER_AGENT,
            'Referer': 'https://www.dongchedi.com/',
            'Accept': 'image/webp,image/apng,image/*,*/*;q=0.8'
        }

        for i, url in enumerate(image_urls, start=1):
            try:
                response = requests.get(url, headers=headers, timeout=10)
                response.raise_for_status()
                ext = self._get_image_extension(url, response.headers.get('content-type', ''))
                image_path = os.path.join(save_dir, f'image_{i}.{ext}')
                with open(image_path, 'wb') as f:
                    f.write(response.content)
                self._log(f"Downloaded image {i}: {image_path}")
            except requests.RequestException as e:
                self._log(f"Error downloading {url}: {e}")
            time.sleep(0.5)  # polite delay

    # ----------------------------
    # Private helpers
    # ----------------------------

    def _log(self, message: str):
        """Send a progress message to the queue or print if no queue."""
        if self.queue:
            self.queue.put(("message", message))
        else:
            print(message)

    def _extract_title(self, page) -> str:
        return page.locator('.line-1.tw-flex-1').inner_text()

    def _extract_price(self, page) -> float:
        text = page.locator(self.PRICE_SELECTOR).inner_text()
        return self._text_to_float(text)

    def _extract_mileage(self, page) -> float:
        text = page.locator(self.MILEAGE_BLOCK).locator('p:has-text("\ue531\ue4fc")').inner_text()
        return self._text_to_float(text)

    def _text_to_float(self, text: str) -> float:
        clean_text = ''.join(SPECIAL_CHARS_TO_NUMBERS.get(c, c) for c in text)
        return float(clean_text)

    def _scroll_images(self, page):
        while True:
            button = page.query_selector(self.SCROLL_BUTTON_SELECTOR)
            if not button or self.DISABLED_CLASS in (button.get_attribute('class') or ''):
                break
            button.click()
            page.wait_for_timeout(300)
        self._log("Scrolled through all images")

    def _extract_images(self, page) -> list:
        images = []
        for img in page.query_selector_all(self.IMAGE_SELECTOR):
            src = img.get_attribute('src')
            if not src or 'svg' in src:
                continue
            if src.endswith('webp') and not src.startswith('https'):
                src = 'https:' + src.replace('124x0', '1850x0')
            if src not in images:
                images.append(src)
        return images

    def _get_configuration_info(self, page) -> dict:
        link = page.query_selector(self.CONFIG_LINK_SELECTOR).get_attribute('href')
        url = 'https://dongchedi.com' + link
        return get_data(url)

    def _prepare_storage_and_save(self, car_data: dict) -> str:
        dir_name = 'car_data'
        if os.path.exists(dir_name):
            rmtree(dir_name)
        os.makedirs(dir_name, exist_ok=True)
        with open(os.path.join(dir_name, 'info.json'), 'w', encoding='utf-8') as f:
            json.dump(car_data, f, ensure_ascii=False, indent=4)
        return dir_name

    @staticmethod
    def _get_image_extension(url: str, content_type: str) -> str:
        if 'webp' in content_type:
            return 'webp'
        if 'jpeg' in content_type or 'jpg' in content_type:
            return 'jpg'
        return url.split('.')[-1].lower() if '.' in url else 'webp'
