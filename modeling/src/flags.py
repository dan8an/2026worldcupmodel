TEAM_FLAG_CODES = {
    "MEX": "MX",
    "RSA": "ZA",
    "KOR": "KR",
    "CZE": "CZ",
    "CAN": "CA",
    "BIH": "BA",
    "QAT": "QA",
    "SUI": "CH",
    "BRA": "BR",
    "MAR": "MA",
    "HAI": "HT",
    "SCO": "GB-SCT",
    "USA": "US",
    "PAR": "PY",
    "AUS": "AU",
    "TUR": "TR",
    "GER": "DE",
    "CUW": "CW",
    "CIV": "CI",
    "ECU": "EC",
    "NED": "NL",
    "JPN": "JP",
    "SWE": "SE",
    "TUN": "TN",
    "BEL": "BE",
    "EGY": "EG",
    "IRN": "IR",
    "NZL": "NZ",
    "ESP": "ES",
    "CPV": "CV",
    "KSA": "SA",
    "URU": "UY",
    "FRA": "FR",
    "SEN": "SN",
    "IRQ": "IQ",
    "NOR": "NO",
    "ARG": "AR",
    "ALG": "DZ",
    "AUT": "AT",
    "JOR": "JO",
    "POR": "PT",
    "COD": "CD",
    "UZB": "UZ",
    "COL": "CO",
    "ENG": "GB-ENG",
    "CRO": "HR",
    "GHA": "GH",
    "PAN": "PA",
}


def flag_for_team(team_id: str) -> str:
    code = TEAM_FLAG_CODES[team_id]
    # Subdivision flags have dedicated emoji tag sequences.
    subdivisions = {
        "GB-ENG": "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f",
        "GB-SCT": "\U0001f3f4\U000e0067\U000e0062\U000e0073\U000e0063\U000e0074\U000e007f",
    }
    if code in subdivisions:
        return subdivisions[code]
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)
