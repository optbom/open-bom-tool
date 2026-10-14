"""Small xlsx files shaped like real Agile and Fusion exports (see ingest/profiles)."""

from pathlib import Path

import openpyxl

AGILE_HEADER = [
    "Level", "Number/Số", "Description/Mô tả", "Rev/Phiên bản", "BOM.Qty", "BOM.Unit(Part)/Đơn vị (phần)",
    "BOM.Alternative/Thay thế", "BOM.Priority/Sự ưu tiên", "BOM.Primary Number/Số chính",
    "BOM.Customer Part No/Mã số khách hàng", "Manufacturers.Mfr Name/Tên Nhà sản xuất",
    "Manufacturers.Mfr Part Number/mã nhà sản xuất", "Lifecycle Phase/Vòng giai đoạn",
]

# The ODM's view (right side): Bay A nested in Bay C, SSDs inside an extra Bay B module,
# an ALT fan listed before its primary, a screw the customer doesn't list.
# (level, number, description, rev, qty, alt, priority, primary, customer pn, mfr, mpn)
AGILE_ROWS = [
    ("0", "MT-TRAY", "Compute tray", "005        ECO000001", "", "", "", "", "", "SUPPLIER-A", "ODM-TRAY"),
    ("1", "MT-BOARD", "Main board", "001        ECO000001", "1", "PRI", "", "", "C-BOARD", "ACME", "MPN-BOARD"),
    ("1", "MT-BAYC", "Bay C module", "001        ECO000001", "1", "PRI", "", "", "C-BAYC", "SUPPLIER-A", "ODM-BAYC"),
    ("2", "MT-BAYA", "Bay A module", "001        ECO000001", "1", "PRI", "", "", "C-BAYA", "SUPPLIER-A", "ODM-BAYA"),
    ("2", "MT-SSD", "8TB SSD", "002        ECO000001", "1", "PRI", "", "", "C-SSD", "DRIVECO", "MPN-SSD-A"),
    ("1", "MT-BAYB", "Bay B module", "001        ECO000001", "1", "PRI", "", "", "TBD", "SUPPLIER-A", "ODM-BAYB"),
    ("2", "MT-SSD", "8TB SSD", "002        ECO000001", "4", "PRI", "", "", "C-SSD", "DRIVECO", "MPN-SSD-A"),
    ("1", "MT-FAN-A", "Fan, second source", "001        ECO000001", "2", "ALT", "2", "MT-FAN-P", "C-FAN", "FANCO2", "MPN-FAN-2"),
    ("1", "MT-FAN-P", "Fan", "001        ECO000001", "2", "PRI", "1", "", "C-FAN", "FANCO", "MPN-FAN-1"),
    ("1", "MT-SCREW", "M3 screw", "001        ECO000001", "8", "PRI", "", "", "TBD", "SCREWCO", "MPN-SCREW"),
]

FUSION_HEADER = [
    "Top Item Number", " BOM Level", "Item Number", "Description", "Rev", "SEQ", "QTY", "MPN",
    "Manufacturer", "Part Type", "MFR Part Reference",
]

# The customer's view (left side): one row per approved manufacturer / vendor part.
# (level, item, description, rev, seq, qty, mpn, manufacturer, part type, mfr ref)
FUSION_ROWS = [
    ("1", "C-TRAY", "SERVER TRAY", "4", "0", "1", "C-TRAY", "SUPPLIER-B", "Manufacturer Part", ""),
    ("1", "C-TRAY", "SERVER TRAY", "4", "0", "1", "ODM-TRAY", "SUPPLIER-B", "Vendor Part", ""),
    (".2", "C-BOARD", "BOARD, MAIN", "2", "10", "1", "MPN-BOARD", "ACME", "Manufacturer Part", ""),
    (".2", "C-BOARD", "BOARD, MAIN", "2", "10", "1", "BOARD", "SUPPLIER-B", "Vendor Part", ""),  # = MT-BOARD minus site prefix
    (".2", "C-SSD", "SSD, DATA, 8TB", "2", "20", "4", "MPN-SSD-A", "DRIVECO", "Manufacturer Part", ""),
    (".2", "C-SSD", "SSD, DATA, 8TB", "2", "20", "4", "MPN-SSD-B", "OTHERCO", "Manufacturer Part", ""),
    (".2", "C-BAYA", "BAY A MODULE", "1", "30", "1", "ODM-BAYA", "SUPPLIER-B", "Vendor Part", ""),
    (".2", "C-BAYC", "CENTRAL BAY MODULE", "1", "40", "1", "ODM-BAYC", "SUPPLIER-B", "Vendor Part", ""),
    ("..3", "C-BOOT", "SSD, BOOT, 8TB", "3", "10", "1", "MPN-SSD-A", "DRIVECO", "Manufacturer Part", ""),
    (".2", "C-FAN", "FAN, 40MM", "1", "50", "2", "MPN-FAN-1", "FANCO", "Manufacturer Part", ""),
    (".2", "C-FAN", "FAN, 40MM", "1", "50", "2", "MPN-FAN-2", "FANCO2", "Manufacturer Part", ""),
    (".2", "C-COOLANT", "COOLANT", "11", "60", "1", "MPN-COOL", "COOLCO", "Manufacturer Part", ""),
]


def write_xlsx(path: Path, rows: list[list]) -> str:
    wb = openpyxl.Workbook()
    for r in rows:
        wb.active.append(r)
    wb.save(path)
    return str(path)


def agile_file(path: Path, rows=AGILE_ROWS) -> str:
    body = [[lvl, num, desc, rev, qty, "EA" if qty else "", alt, prio, prim, cust, mfr, mpn, "Production"]
            for lvl, num, desc, rev, qty, alt, prio, prim, cust, mfr, mpn in rows]
    return write_xlsx(path, [AGILE_HEADER, *body])


def fusion_file(path: Path, rows=FUSION_ROWS) -> str:
    title = [["BOM Explosion With MPN/SPN"], ["Item(s): C-TRAY"]]
    body = [["C-TRAY", lvl, item, desc, rev, seq, qty, mpn, mfr, ptype, ref]
            for lvl, item, desc, rev, seq, qty, mpn, mfr, ptype, ref in rows]
    return write_xlsx(path, [*title, FUSION_HEADER, *body])


def agile_rev2_rows():
    """The ODM's next export: board qty 1 -> 2, and the second-source fan switches MPN."""
    rows = []
    for r in AGILE_ROWS:
        r = list(r)
        if r[1] == "MT-BOARD":
            r[4] = "2"
        if r[1] == "MT-FAN-A":
            r[3], r[10] = "002        ECO000002", "MPN-FAN-9"
        rows.append(tuple(r))
    return rows
