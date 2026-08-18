from fastapi import FastAPI
import re
import html
from urllib.parse import unquote, urlparse

app = FastAPI()


ALLOWED_HOSTS = {
    "cdn-6h89i6g.example",
    "app-7lgcpyd.example"
}


def result(reason):
    return {
        "safe": reason == "SAFE",
        "reason": reason
    }


def decode_once(text):

    x = unquote(text)

    x = html.unescape(x)

    x = re.sub(
        r'\\u([0-9a-fA-F]{4})',
        lambda m: chr(int(m.group(1),16)),
        x
    )

    return x



def dangerous_scheme(text):

    return re.search(
        r'(javascript|data|vbscript)\s*:',
        text,
        re.I
    )


def check_urls(urls):

    for url in urls:

        if url.startswith("//"):
            url="https:"+url


        parsed=urlparse(url)


        if parsed.scheme and parsed.scheme not in [
            "http","https"
        ]:
            return "DANGEROUS_SCHEME"


        if parsed.hostname:

            if parsed.hostname not in ALLOWED_HOSTS:
                return "EXTERNAL_EXFIL"

    return None



@app.post("/sanitize-output")
def sanitize(data:dict):


    # 1 Schema

    if not isinstance(data,dict):
        return result("INVALID_SCHEMA")


    if data.get("channel") not in [
        "html",
        "markdown",
        "url",
        "sql",
        "shell"
    ]:
        return result("INVALID_SCHEMA")


    if not isinstance(data.get("output"),str):
        return result("INVALID_SCHEMA")


    text=data["output"]


    if len(text)>20000:
        return result("INVALID_SCHEMA")



    # 2 Encoded payload

    decoded=decode_once(text)

    if decoded != text:

        # recursively test dangerous patterns
        if (
            re.search(r"<\s*(script|iframe|object|embed)",
                      decoded,re.I)
            or
            re.search(r"\bon[a-z]+\s*=",
                      decoded,re.I)
            or
            dangerous_scheme(decoded)
        ):
            return result("ENCODED_PAYLOAD")



    channel=data["channel"]



    # HTML

    if channel=="html":

        if re.search(
            r"<\s*(script|iframe|object|embed)",
            text,
            re.I
        ):
            return result("SCRIPT_TAG")


        if re.search(
            r"\bon[a-z]+\s*=",
            text,
            re.I
        ):
            return result("EVENT_HANDLER")


        if dangerous_scheme(text):
            return result("DANGEROUS_SCHEME")


        urls=re.findall(
            r'(?:src|href)\s*=\s*["\']([^"\']+)',
            text,
            re.I
        )


        issue=check_urls(urls)

        if issue:
            return result(issue)



    # Markdown

    elif channel=="markdown":

        if dangerous_scheme(text):
            return result("DANGEROUS_SCHEME")


        urls=re.findall(
            r'\]\((.*?)\)',
            text
        )


        issue=check_urls(urls)

        if issue:
            return result(issue)



    # URL

    elif channel=="url":

        if dangerous_scheme(text):
            return result("DANGEROUS_SCHEME")


        issue=check_urls([text.strip()])

        if issue:
            return result(issue)



    # SQL

    elif channel=="sql":

        if re.search(
            r"('|\"|;|--|/\*|\bunion\b|\bor\s+1=1\b)",
            text,
            re.I
        ):
            return result("SQL_METACHAR")



    # Shell

    elif channel=="shell":

        if re.search(
            r"[;&|`<>]|\$\(|\$\{",
            text
        ):
            return result("SHELL_METACHAR")


    return result("SAFE")