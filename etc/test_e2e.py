#!/usr/bin/env python3

# Runs end-to-end tests on a jupyterhub instance

import re
import sys
import json
import time
import asyncio
import argparse
from urllib.parse import urljoin

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
import requests
import websockets

MAX_LOAD_COUNT = 10

PACHCTL_LOGIN_PATTERN = re.compile(r'You are "github:admin"\r\nsession expires: (.+)\r\nYou are an administrator of this Pachyderm cluster\r\n', re.MULTILINE)
PYTHON_LOGIN_PATTERN = re.compile(r'username: \"github:admin\"')

def retry(f, attempts=10, sleep=1.0):
    count = 0
    while count < attempts:
        try:
            return f()
        except AssertionError:
            count += 1
            if count >= attempts:
                raise
            time.sleep(sleep)

def login(driver, url, username, password):
    print("login")

    # get the jupyterhub login page
    driver.get(url)

    # fill out username/password fields
    username_field = driver.find_element_by_id("username_input")
    username_field.send_keys(username)
    password_field = driver.find_element_by_id("password_input")
    password_field.send_keys(password)
    driver.find_element_by_id("login_submit").click()

    # Repeatedly check for the title on the jupyter user homepage. We
    # repeatedly check over a period of 30s because, on the first login,
    # jupyterhub shows a loading page while the user pod is spun up. We want
    # to ensure it successfully clears this loading page and gets to the
    # homepage.
    def check_title():
        assert driver.title == "Home Page - Select or create a notebook", "not in the user homepage"
    retry(check_title, attempts=30)

def get_token(driver, url):
    print("token")

    driver.get(urljoin(url, "/hub/token"))

    def get_token():
        driver.find_element_by_class_name("btn-jupyter").click()
        token = driver.find_element_by_id("token-result").get_attribute("innerHTML")
        assert token, "token not ready yet"
        return token
    return retry(get_token)

async def run_command(ws, cmd, timeout=1.0):
    await ws.send(json.dumps(["stdin", "{}\r\n".format(cmd)]))
    await ws.recv() # ignore command being echoed back

    start_time = time.time()
    lines = []

    while time.time() - start_time < timeout:
        try:
            line = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        else:
            lines.append(json.loads(line))

    return lines

async def test_terminal(url, token):
    res = requests.request("POST", urljoin(url, "/user/github%3Aadmin/api/terminals"), data=dict(token=token))
    res.raise_for_status()
    term_name = res.json()["name"]

    ws_url = urljoin(url, "/user/github%3Aadmin/terminals/websocket/{}?token={}".format(term_name, token))
    ws_url = ws_url.replace("http://", "ws://")
    ws_url = ws_url.replace("https://", "wss://")
    async with websockets.connect(ws_url) as ws:
        await ws.recv() # ignore setup message
        
        print("pachctl")
        lines = await run_command(ws, "pachctl auth whoami")
        stdout = "".join([l for (io, l) in lines if io == 'stdout'])
        assert PACHCTL_LOGIN_PATTERN.search(stdout) is not None, \
            "unexpected terminal output\n{}".format(json.dumps(lines, indent=2))

        print("python_pachyderm")
        lines = await run_command(ws, "python3 -c 'import python_pachyderm; c = python_pachyderm.Client.new_in_cluster(); print(c.who_am_i())'")
        stdout = "".join([l for (io, l) in lines if io == 'stdout'])
        assert PYTHON_LOGIN_PATTERN.search(stdout) is not None, \
            "unexpected terminal output\n{}".format(json.dumps(lines, indent=2))

def main(url, username, password, webdriver_path, headless, debug):
    opts = Options()
    opts.headless = headless

    # create a selenium driver
    if webdriver_path:
        driver = webdriver.Firefox(executable_path=webdriver_path, options=opts)
    else:
        driver = webdriver.Firefox(options=opts)
    
    login(driver, url, username, password)
    token = get_token(driver, url)
    asyncio.run(test_terminal(url, token))

    if not debug:
        driver.quit()

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("url", help="JupyterHub login url")
    parser.add_argument("username", help="JupyterHub login username")
    parser.add_argument("password", help="JupyterHub login password")
    parser.add_argument("--webdriver", help="path to webdriver executable")
    parser.add_argument("--headless", action="store_true", help="headless mode")
    parser.add_argument("--debug", action="store_true", help="debug mode")
    args = parser.parse_args()

    main(args.url, args.username, args.password, args.webdriver, args.headless, args.debug)