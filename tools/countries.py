"""Country dimension for the hedge/asset-manager tracker.

Only the US has a free, per-holding disclosure feed (SEC 13F) that this tool can
turn into buy/sell tables. For every other country there is no free equivalent,
so those managers are tracked via news only (Google News RSS). Japan can be
extended with real filings if an EDINET API key is provided (see tools/edinet.py).

Each entry: country code -> {name, flag, managers[]}. `managers` are famous
funds / asset managers used to fetch news; they are not SEC CIKs.
"""
from __future__ import annotations

# US is handled separately from config.yml (real 13F holdings). Listed here only
# for the country selector metadata.
COUNTRIES: dict[str, dict] = {
    "US": {"name": "United States", "flag": "\U0001F1FA\U0001F1F8", "managers": []},
    "IN": {
        "name": "India", "flag": "\U0001F1EE\U0001F1F3",
        "managers": [
            "HDFC Asset Management", "SBI Mutual Fund", "ICICI Prudential AMC",
            "Nippon India Mutual Fund", "Kotak Mahindra Asset Management",
            "Axis Mutual Fund", "Aditya Birla Sun Life AMC", "DSP Mutual Fund",
            "Motilal Oswal Asset Management", "Quant Mutual Fund",
        ],
    },
    "JP": {
        "name": "Japan", "flag": "\U0001F1EF\U0001F1F5",
        "managers": [
            "Nomura Asset Management", "Government Pension Investment Fund GPIF",
            "Daiwa Asset Management", "Nikko Asset Management",
            "Sumitomo Mitsui Trust Asset Management", "Asset Management One",
            "Mitsubishi UFJ Asset Management", "SoftBank Vision Fund",
        ],
    },
    "SE": {
        "name": "Sweden", "flag": "\U0001F1F8\U0001F1EA",
        "managers": [
            "EQT AB", "Cevian Capital", "Investor AB", "AP7 Sjunde AP-fonden",
            "Swedbank Robur", "Handelsbanken Fonder", "Lundbergs", "Nordea Fonder",
        ],
    },
    "DK": {
        "name": "Denmark", "flag": "\U0001F1E9\U0001F1F0",
        "managers": [
            "Novo Holdings", "ATP pension Denmark", "Danske Invest",
            "PFA Pension", "Maj Invest", "Nordea Invest Denmark",
            "PensionDanmark", "Jyske Invest",
        ],
    },
    "NO": {
        "name": "Norway", "flag": "\U0001F1F3\U0001F1F4",
        "managers": [
            "Norges Bank Investment Management", "Storebrand Asset Management",
            "DNB Asset Management", "KLP Kapitalforvaltning", "Folketrygdfondet",
            "Skagen Funds", "Odin Forvaltning",
        ],
    },
    "FR": {
        "name": "France", "flag": "\U0001F1EB\U0001F1F7",
        "managers": [
            "Amundi", "AXA Investment Managers", "BNP Paribas Asset Management",
            "Carmignac", "Comgest", "Tikehau Capital", "Ostrum Asset Management",
            "Eurazeo",
        ],
    },
    "DE": {
        "name": "Germany", "flag": "\U0001F1E9\U0001F1EA",
        "managers": [
            "DWS Group", "Allianz Global Investors", "Union Investment",
            "Deka Investment", "Flossbach von Storch", "DJE Kapital",
            "Lupus alpha", "MEAG",
        ],
    },
    "IE": {
        "name": "Ireland", "flag": "\U0001F1EE\U0001F1EA",
        "managers": [
            "Setanta Asset Management", "Davy Asset Management",
            "Irish Life Investment Managers", "KBI Global Investors",
            "Mediolanum International Funds", "Zurich Life Ireland",
            "New Ireland Assurance",
        ],
    },
    "GB": {
        "name": "United Kingdom", "flag": "\U0001F1EC\U0001F1E7",
        "managers": [
            "Man Group", "Schroders", "Baillie Gifford", "Lansdowne Partners",
            "Lindsell Train", "Ruffer LLP", "Marshall Wace", "Pelham Capital",
        ],
    },
    "CN": {
        "name": "China", "flag": "\U0001F1E8\U0001F1F3",
        "managers": [
            "Hillhouse Capital", "High-Flyer Quant", "China Asset Management",
            "E Fund Management", "Bosera Asset Management",
            "Harvest Fund Management", "Southern Asset Management", "HongShan",
        ],
    },
}

# Order for the country selector (US first / default).
COUNTRY_ORDER = ["US", "IN", "JP", "SE", "DK", "NO", "FR", "DE", "IE", "GB", "CN"]

# Famous listed stocks per country -> (ticker, company name for news search).
# Used for the "News Signals" buy/sell scan.
STOCKS: dict[str, list[tuple[str, str]]] = {
    "US": [
        ("NVDA", "Nvidia"), ("TSLA", "Tesla"), ("AAPL", "Apple"), ("AMZN", "Amazon"),
        ("MSFT", "Microsoft"), ("GOOGL", "Alphabet"), ("META", "Meta Platforms"),
        ("AMD", "AMD"), ("NFLX", "Netflix"), ("PLTR", "Palantir"), ("COIN", "Coinbase"),
        ("MSTR", "MicroStrategy"), ("AVGO", "Broadcom"), ("INTC", "Intel"),
        ("MU", "Micron"), ("SMCI", "Super Micro Computer"), ("UBER", "Uber"),
        ("DIS", "Disney"),
    ],
    "IN": [
        ("RELIANCE", "Reliance Industries"), ("TCS", "Tata Consultancy Services"),
        ("HDFCBANK", "HDFC Bank"), ("INFY", "Infosys"), ("ICICIBANK", "ICICI Bank"),
        ("BHARTIARTL", "Bharti Airtel"), ("ADANIENT", "Adani Enterprises"),
    ],
    "JP": [
        ("7203", "Toyota Motor"), ("6758", "Sony Group"), ("9984", "SoftBank Group"),
        ("6861", "Keyence"), ("7974", "Nintendo"), ("8306", "Mitsubishi UFJ"),
        ("8035", "Tokyo Electron"),
    ],
    "SE": [
        ("ATCO-A", "Atlas Copco"), ("VOLV-B", "Volvo"), ("ERIC", "Ericsson"),
        ("INVE-B", "Investor AB"), ("SPOT", "Spotify"), ("EQT", "EQT AB"),
        ("HM-B", "H&M Hennes Mauritz"),
    ],
    "DK": [
        ("NOVO-B", "Novo Nordisk"), ("MAERSK-B", "Maersk"), ("DSV", "DSV"),
        ("VWS", "Vestas Wind Systems"), ("CARL-B", "Carlsberg"),
        ("COLO-B", "Coloplast"), ("ORSTED", "Orsted"),
    ],
    "NO": [
        ("EQNR", "Equinor"), ("DNB", "DNB Bank"), ("NHY", "Norsk Hydro"),
        ("TEL", "Telenor"), ("AKRBP", "Aker BP"), ("MOWI", "Mowi"), ("YAR", "Yara"),
    ],
    "FR": [
        ("MC", "LVMH"), ("TTE", "TotalEnergies"), ("SAN", "Sanofi"),
        ("AIR", "Airbus"), ("SU", "Schneider Electric"), ("OR", "L'Oreal"),
        ("BNP", "BNP Paribas"),
    ],
    "DE": [
        ("SAP", "SAP"), ("SIE", "Siemens"), ("VOW3", "Volkswagen"),
        ("ALV", "Allianz"), ("MBG", "Mercedes-Benz"), ("DTE", "Deutsche Telekom"),
        ("BAS", "BASF"),
    ],
    "IE": [
        ("RYA", "Ryanair"), ("CRH", "CRH"), ("KRZ", "Kerry Group"),
        ("KNGSPAN", "Kingspan"), ("FLTR", "Flutter Entertainment"),
        ("BIRG", "Bank of Ireland"), ("A5G", "AIB Group"),
    ],
    "GB": [
        ("AZN", "AstraZeneca"), ("SHEL", "Shell"), ("HSBA", "HSBC"),
        ("ULVR", "Unilever"), ("BP", "BP"), ("GSK", "GSK"),
        ("RR", "Rolls-Royce"),
    ],
    "CN": [
        ("BABA", "Alibaba"), ("0700", "Tencent"), ("BYDDY", "BYD"),
        ("PDD", "PDD Holdings"), ("3690", "Meituan"), ("BIDU", "Baidu"),
        ("JD", "JD.com"),
    ],
}

# Well-known market commentators / analysts per country for the "Analyst" panel.
PUNDITS: dict[str, list[str]] = {
    "US": ["Jim Cramer", "Dan Ives", "Tom Lee", "Cathie Wood", "Gene Munster", "Mad Money"],
    "IN": ["Raamdeo Agrawal", "Porinju Veliyath", "Samir Arora", "Nilesh Shah"],
    "JP": ["Masayoshi Son", "Nomura strategist"],
    "SE": ["Christer Gardell"],
    "DK": [],
    "NO": ["Norges Bank Nicolai Tangen"],
    "FR": ["Carmignac strategist"],
    "DE": ["Flossbach von Storch strategist"],
    "IE": [],
    "GB": ["Terry Smith", "Nick Train", "Ruffer strategist"],
    "CN": ["Hillhouse Zhang Lei"],
}


def country_stocks(code: str) -> list[tuple[str, str]]:
    return STOCKS.get(code, [])


def country_pundits(code: str) -> list[str]:
    return PUNDITS.get(code, [])


def international_managers() -> list[tuple[str, str]]:
    """Return (manager_name, country_code) for every non-US country."""
    out: list[tuple[str, str]] = []
    for code in COUNTRY_ORDER:
        if code == "US":
            continue
        for name in COUNTRIES[code]["managers"]:
            out.append((name, code))
    return out
