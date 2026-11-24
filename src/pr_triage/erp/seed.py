from contextlib import closing
from pathlib import Path

from pr_triage.erp.store import ErpStore

PEOPLE = [
    ("ana.silva@exemplo.pt", "Ana Silva", "requester", "CC-TI-100"),
    ("rui.costa@exemplo.pt", "Rui Costa", "requester", "CC-FAC-200"),
    ("sofia.matos@exemplo.pt", "Sofia Matos", "requester", "CC-LAB-300"),
    ("bruno.pires@exemplo.pt", "Bruno Pires", "requester", "CC-MKT-400"),
    ("tiago.lopes@exemplo.pt", "Tiago Lopes", "requester", None),
    ("joao.ferreira@exemplo.pt", "João Ferreira", "department_head", "CC-TI-100"),
    ("maria.santos@exemplo.pt", "Maria Santos", "director", None),
    ("pedro.alves@exemplo.pt", "Pedro Alves", "purchasing", None),
    ("helena.dias@exemplo.pt", "Helena Dias", "controlling", None),
]

COST_CENTERS = [
    ("CC-TI-100", "Tecnologias de Informação", "TI", "joao.ferreira@exemplo.pt", 1),
    ("CC-FAC-200", "Instalações", "Instalações", "rui.costa@exemplo.pt", 1),
    ("CC-LAB-300", "Laboratório", "Qualidade", "sofia.matos@exemplo.pt", 1),
    ("CC-MKT-400", "Marketing", "Marketing", "bruno.pires@exemplo.pt", 1),
    ("CC-OPS-500", "Operações", "Operações", "tiago.lopes@exemplo.pt", 1),
    ("CC-OLD-900", "Projeto encerrado", "Operações", "tiago.lopes@exemplo.pt", 0),
]

BUDGETS = [
    ("CC-TI-100", 150000.0, 38000.0),
    ("CC-FAC-200", 60000.0, 41000.0),
    ("CC-LAB-300", 25000.0, 24200.0),
    ("CC-MKT-400", 30000.0, 6000.0),
    ("CC-OPS-500", 80000.0, 20000.0),
]

SUPPLIERS = [
    ("S01", "TechNova", 1),
    ("S02", "OfficeMax Lda", 1),
    ("S03", "LabSupply PT", 1),
    ("S04", "CleanPro", 1),
    ("S05", "PrintAd", 1),
    ("S06", "Quick Parts", 0),
    ("S07", "Consultores Atlântico", 1),
]

CONTRACTS = [
    ("FC-001", "S01", "it_equipment", "{y}-01-01", "{y}-12-31", 200000.0, 80000.0),
    ("FC-002", "S04", "cleaning", "{p}-01-01", "{p}-12-31", 15000.0, 9000.0),
    ("FC-003", "S02", "furniture", "{y}-03-01", "{n}-02-28", 50000.0, 10000.0),
    ("FC-004", "S05", "marketing", "{y}-01-01", "{y}-12-31", 20000.0, 19500.0),
]

CATALOG = [
    ("LT-14", "Portátil 14 polegadas TechNova Pro", "it_equipment", "S01", 1150.0, 1),
    ("LT-16", "Portátil 16 polegadas TechNova Max", "it_equipment", "S01", 1480.0, 1),
    ("MON-27", "Monitor 27 polegadas", "it_equipment", "S01", 289.0, 1),
    ("KB-01", "Teclado sem fios", "it_equipment", "S01", 39.0, 1),
    ("DSK-01", "Secretária ajustável", "furniture", "S02", 320.0, 1),
    ("CHR-01", "Cadeira ergonómica", "furniture", "S02", 245.0, 1),
    ("CHR-02", "Cadeira ergonómica premium", "furniture", "S02", 410.0, 0),
    ("PIP-10", "Pipetas, caixa de 10", "lab_consumables", "S03", 74.0, 1),
    ("GLV-01", "Luvas de nitrilo, caixa", "lab_consumables", "S03", 18.0, 1),
    ("CLN-01", "Kit de limpeza de escritório", "cleaning", "S04", 55.0, 1),
    ("FLY-1K", "Flyers, 1000 unidades", "marketing", "S05", 120.0, 1),
    ("BOX-50", "Caixas de cartão, 50 unidades", "packaging", "S06", 35.0, 1),
]


def seed(store: ErpStore) -> None:
    year = store.clock().year
    fill = {"y": year, "p": year - 1, "n": year + 1}
    with closing(store.connect()) as conn, conn:
        conn.executemany("INSERT OR REPLACE INTO people VALUES (?, ?, ?, ?)", PEOPLE)
        conn.executemany("INSERT OR REPLACE INTO cost_centers VALUES (?, ?, ?, ?, ?)", COST_CENTERS)
        conn.executemany(
            "INSERT OR REPLACE INTO budgets VALUES (?, ?, ?, ?)",
            [(code, year, total, committed) for code, total, committed in BUDGETS],
        )
        conn.executemany("INSERT OR REPLACE INTO suppliers VALUES (?, ?, ?)", SUPPLIERS)
        conn.executemany(
            "INSERT OR REPLACE INTO framework_contracts VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (cid, sid, cat, start.format(**fill), end.format(**fill), mx, used)
                for cid, sid, cat, start, end, mx, used in CONTRACTS
            ],
        )
        conn.executemany("INSERT OR REPLACE INTO catalog_items VALUES (?, ?, ?, ?, ?, ?)", CATALOG)


def create_demo_erp(path: str | Path, **kwargs) -> ErpStore:
    Path(path).unlink(missing_ok=True)
    store = ErpStore(path, **kwargs)
    seed(store)
    return store
