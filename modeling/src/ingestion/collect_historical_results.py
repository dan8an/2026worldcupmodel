import argparse
import hashlib
import json
import ssl
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import certifi

from ..data import ROOT

DEFAULT_URL = (
    "https://raw.githubusercontent.com/martj42/"
    "international_results/master/results.csv"
)
OUTPUT = ROOT / "data" / "raw" / "international_results.csv"
METADATA = ROOT / "data" / "raw" / "international_results.metadata.json"


def collect(url: str = DEFAULT_URL, output: Path = OUTPUT) -> Path:
    request = Request(url, headers={"User-Agent": "2026worldcupmodel/0.1"})
    tls_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=30, context=tls_context) as response:
        payload = response.read()
    if not payload.startswith(b"date,home_team,away_team"):
        raise ValueError("Historical results payload has an unexpected schema")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)
    metadata = {
        "source_url": url,
        "license": "CC0-1.0",
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
    }
    METADATA.write_text(json.dumps(metadata, indent=2))
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    print(collect(args.url, args.output))
