"""Presidio 2.2.364 IBAN patterns (MIT, Microsoft)."""

CK = "[0-9]{2}[ ]?"

A = "[A-Z][ ]?"
A3 = "([A-Z][ ]?){3}"
A4 = "([A-Z][ ]?){4}"

C = "[a-zA-Z0-9][ ]?"
C2 = "([a-zA-Z0-9][ ]?){2}"
C3 = "([a-zA-Z0-9][ ]?){3}"
C4 = "([a-zA-Z0-9][ ]?){4}"

N = "[0-9][ ]?"
N2 = "([0-9][ ]?){2}"
N3 = "([0-9][ ]?){3}"
N4 = "([0-9][ ]?){4}"

regex_per_country = {
    "AL": "(AL)" + CK + N4 + N4 + C4 + C4 + C4 + C4,
    "AD": "(AD)" + CK + N4 + N4 + C4 + C4 + C4,
    "AT": "(AT)" + CK + N4 + N4 + N4 + N4,
    "AZ": "(AZ)" + CK + C4 + N4 + N4 + N4 + N4 + N4,
    "BH": "(BH)" + CK + A4 + C4 + C4 + C4 + C2,
    "BY": "(BY)" + CK + C4 + N4 + C4 + C4 + C4 + C4,
    "BE": "(BE)" + CK + N4 + N4 + N4,
    "BA": "(BA)" + CK + N4 + N4 + N4 + N4,
    "BR": "(BR)" + CK + N4 + N4 + N4 + N4 + N4 + N3 + A + C,
    "BG": "(BG)" + CK + A4 + N4 + N + N + C2 + C4 + C2,
    "CR": "(CR)" + CK + "[0]" + N3 + N4 + N4 + N4 + N2,
    "HR": "(HR)" + CK + N4 + N4 + N4 + N4 + N,
    "CY": "(CY)" + CK + N4 + N4 + C4 + C4 + C4 + C4,
    "CZ": "(CZ)" + CK + N4 + N4 + N4 + N4 + N4,
    "DK": "(DK)" + CK + N4 + N4 + N4 + N2,
    "DO": "(DO)" + CK + A4 + N4 + N4 + N4 + N4 + N4,
    "TL": "(TL)" + CK + N4 + N4 + N4 + N4 + N3,
    "EE": "(EE)" + CK + N4 + N4 + N4 + N4,
    "FO": "(FO)" + CK + N4 + N4 + N4 + N2,
    "FI": "(FI)" + CK + N4 + N4 + N4 + N2,
    "FR": "(FR)" + CK + N4 + N4 + N2 + C2 + C4 + C4 + C + N2,
    "GE": "(GE)" + CK + C2 + N2 + N4 + N4 + N4 + N2,
    "DE": "(DE)" + CK + N4 + N4 + N4 + N4 + N2,
    "GI": "(GI)" + CK + A4 + C4 + C4 + C4 + C3,
    "GR": "(GR)" + CK + N4 + N3 + C + C4 + C4 + C4 + C3,
    "GL": "(GL)" + CK + N4 + N4 + N4 + N2,
    "GT": "(GT)" + CK + C4 + C4 + C4 + C4 + C4 + C4,
    "HU": "(HU)" + CK + N4 + N4 + N4 + N4 + N4 + N4,
    "IS": "(IS)" + CK + N4 + N4 + N4 + N4 + N4 + N2,
    "IE": "(IE)" + CK + C4 + N4 + N4 + N4 + N2,
    "IL": "(IL)" + CK + N4 + N4 + N4 + N4 + N3,
    "IT": "(IT)" + CK + A + N3 + N4 + N3 + C + C3 + C + C4 + C3,
    "JO": "(JO)" + CK + A4 + N4 + N4 + N4 + N4 + N4 + N2,
    "KZ": "(KZ)" + CK + N3 + C + C4 + C4 + C4,
    "XK": "(XK)" + CK + N4 + N4 + N4 + N4,
    "KW": "(KW)" + CK + A4 + C4 + C4 + C4 + C4 + C4 + C2,
    "LV": "(LV)" + CK + A4 + C4 + C4 + C4 + C,
    "LB": "(LB)" + CK + N4 + C4 + C4 + C4 + C4 + C4,
    "LI": "(LI)" + CK + N4 + N + C3 + C4 + C4 + C,
    "LT": "(LT)" + CK + N4 + N4 + N4 + N4,
    "LU": "(LU)" + CK + N3 + C + C4 + C4 + C4,
    "MT": "(MT)" + CK + A4 + N4 + N + C3 + C4 + C4 + C4 + C3,
    "MR": "(MR)" + CK + N4 + N4 + N4 + N4 + N4 + N3,
    "MU": "(MU)" + CK + A4 + N4 + N4 + N4 + N4 + N3 + A,
    "MD": "(MD)" + CK + C4 + C4 + C4 + C4 + C4,
    "MC": "(MC)" + CK + N4 + N4 + N2 + C2 + C4 + C4 + C + N2,
    "ME": "(ME)" + CK + N4 + N4 + N4 + N4 + N2,
    "NL": "(NL)" + CK + A4 + N4 + N4 + N2,
    "MK": "(MK)" + CK + N3 + C + C4 + C4 + C + N2,
    "NO": "(NO)" + CK + N4 + N4 + N3,
    "PK": "(PK)" + CK + C4 + N4 + N4 + N4 + N4,
    "PS": "(PS)" + CK + C4 + N4 + N4 + N4 + N4 + N,
    "PL": "(PL)" + CK + N4 + N4 + N4 + N4 + N4 + N4,
    "PT": "(PT)" + CK + N4 + N4 + N4 + N4 + N,
    "QA": "(QA)" + CK + A4 + C4 + C4 + C4 + C4 + C,
    "RO": "(RO)" + CK + A4 + C4 + C4 + C4 + C4,
    "SM": "(SM)" + CK + A + N3 + N4 + N3 + C + C4 + C4 + C3,
    "SA": "(SA)" + CK + N2 + C2 + C4 + C4 + C4 + C4,
    "RS": "(RS)" + CK + N4 + N4 + N4 + N4 + N2,
    "SK": "(SK)" + CK + N4 + N4 + N4 + N4 + N4,
    "SI": "(SI)" + CK + N4 + N4 + N4 + N3,
    "ES": "(ES)" + CK + N4 + N4 + N4 + N4 + N4,
    "SE": "(SE)" + CK + N4 + N4 + N4 + N4 + N4,
    "CH": "(CH)" + CK + N4 + N + C3 + C4 + C4 + C,
    "TN": "(TN)" + CK + N4 + N4 + N4 + N4 + N4,
    "TR": "(TR)" + CK + N4 + N + C3 + C4 + C4 + C4 + C2,
    "AE": "(AE)" + CK + N4 + N4 + N4 + N4 + N3,
    "GB": "(GB)" + CK + A4 + N4 + N4 + N4 + N2,
    "VA": "(VA)" + CK + N4 + N4 + N4 + N4 + N2,
    "VG": "(VG)" + CK + C4 + N4 + N4 + N4 + N4,
    "EG": "(EG)" + CK + N4 + N4 + N4 + N4 + N4 + N4 + N,
    "IQ": "(IQ)" + CK + A4 + N4 + N4 + N4 + N3,
    "LY": "(LY)" + CK + N4 + N4 + N4 + N4 + N4 + N,
    "LC": "(LC)" + CK + A4 + C4 + C4 + C4 + C4 + C4 + C4,
    "SC": "(SC)" + CK + A4 + N4 + N4 + N4 + N4 + N4 + A3,
    "UA": "(UA)" + CK + N4 + N2 + C4 + C4 + C4 + C4 + C3,
}
