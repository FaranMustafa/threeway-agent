"""Build valid X12 interchanges (ISA/GS/ST...SE/GE/IEA) for sample data and tests."""

from __future__ import annotations

FUNCTIONAL_ID = {"850": "PO", "856": "SH", "810": "IN"}


def _isa(sender: str, receiver: str, control: int, date: str) -> str:
    return "*".join(
        [
            "ISA",
            "00",
            " " * 10,
            "00",
            " " * 10,
            "ZZ",
            sender.ljust(15),
            "ZZ",
            receiver.ljust(15),
            date[2:],
            "1200",
            "U",
            "00401",
            f"{control:09d}",
            "0",
            "P",
            ">",
        ]
    )


def build(
    code: str,
    transactions: list[list[str]],
    sender: str = "VENDOR",
    receiver: str = "BUYER",
    control: int = 1,
    date: str = "20260901",
) -> str:
    """Each transaction is a list of body segments like 'BEG*00*SA*PO-1**20260901'."""
    segments = [_isa(sender, receiver, control, date)]
    segments.append(f"GS*{FUNCTIONAL_ID[code]}*{sender}*{receiver}*{date}*1200*{control}*X*004010")
    for n, body in enumerate(transactions, start=1):
        st_control = f"{n:04d}"
        segments.append(f"ST*{code}*{st_control}")
        segments.extend(body)
        segments.append(f"SE*{len(body) + 2}*{st_control}")
    segments.append(f"GE*{len(transactions)}*{control}")
    segments.append(f"IEA*1*{control:09d}")
    return "~\n".join(segments) + "~\n"
