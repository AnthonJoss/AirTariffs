"""Valores por defecto por cliente, tomados de los Excel históricos (excels/).

provider_id / airline_id son ids de `companies`. Si ningún perfil coincide, el usuario los elige en la revisión.
"""
import re

EGYPT_COMMENTS = (
    "<p>EGP 1 •Minimum must be reached before 100kg rate applies<br>"
    "•Rates are ALL-IN (including Fuel & Security) Excluding EDI charges & transit Shipment fee<br>"
    "•Transit Shipment Fee: $.05/kg with a $25.00 min (not applicable for Final destination CAI).<br>"
    "•Minimum Shipment Size of 100kg must be shown on the AWB otherwise TACT rates apply<br>"
    "•Known Shipper, Lower Deck, General Cargo only<br>"
    "•Loose rates based on average density of 1:9 (DG6 IATA)<br>"
    "•Not valid for Special Cargo<br>"
    "•Excludes Oversized Cargo (shipments containing pieces whose length exceeds 125 inches or width exceeds 96 inches)<br>"
    "•AWB execution date, not flown date, determines the validity of this agreement<br>"
    "•Rates are subject to capacity availability and rates are subject to change<br>"
    "•“As Agreed” mawb are not accepted</p>"
)

ITA_COMMENTS = (
    "<p>ITA 1.3 Rates all in, valid for General Cargo, Known Shipper only, "
    "screening is additional $0.10 if tendered unscreened</p>"
    "<p>SFO drop station: 632 West Field Rd<br>"
    "Narrowbody service: MAX DIMS (inches) 61”L X 59W X 42”H – max pc weight 900Kg ()</p>"
)

PROFILES = [
    {
        "name": "EgyptAir",
        "match": r"egypt|ms [a-z]{3}[a-z ]* promo|spot rate code|promo pricing",
        "provider_id": 8265,  # ATC Aviation
        "airline_id": 8296,   # Egypt Air
        "commodity_id": 4,
        "airchaft": 2,        # PAX
        "cost": 0,
        "offer": 1,
        "comments": EGYPT_COMMENTS,
        # Los PDFs nuevos no traen columna MIN; en los Excel MIN = 100 kg x tarifa de +100 (240 = 100 x 2.4)
        "min_rule": "100kg_x_rate100",
    },
    {
        "name": "ITA Airways",
        "match": r"\bita\b|ita[_ ]promo|ita airways",
        "provider_id": 8265,  # ATC Aviation
        "airline_id": 8299,   # ITA Airways Cargo
        "commodity_id": 4,
        "airchaft": 2,
        "cost": 0.1,
        "offer": 1,
        "comments": ITA_COMMENTS,
        # Cada tarifa ITA lleva un Screen Fee de 0.10/kg (tariff_feeds, fee_id 101)
        "fees": [{"fee_id": 101, "fee_comment": None, "cost_unit": 0.1}],
    },
    {
        "name": "Avianca (rate sheet Next Logistics)",
        "match": r"agent name:\s*next logistics|next logistics group .*rate sheet",
        "provider_id": 3081,  # Avianca Cargo
        "airline_id": 3081,
        "commodity_id": 4,
        "airchaft": 1,        # CAO
        "cost": 0,
        "offer": 0,
        "comments": "<p>ALLIN</p>",
        # La columna "Fuel x KG" del PDF se carga por fila como fee FSC (fee_id 13), no se suma a la tarifa
        "fuel_fee_id": 13,
    },
]


def detect(filename: str, text: str) -> dict | None:
    haystack = f"{filename}\n{text[:2000]}".lower()
    for p in PROFILES:
        if re.search(p["match"], haystack):
            return {k: v for k, v in p.items() if k != "match"}
    return None
