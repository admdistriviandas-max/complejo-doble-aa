import aiosqlite
import os
from datetime import date, timedelta

DB_PATH = os.getenv("DB_PATH", "doble_aa.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS clientes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    telefono TEXT,
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS reservas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,          -- YYYY-MM-DD
    hora TEXT NOT NULL,           -- "17", "18", ... "23"
    cancha INTEGER NOT NULL,      -- 1 o 2
    cliente_id INTEGER,
    cliente_nombre TEXT NOT NULL,
    estado TEXT DEFAULT 'señado', -- libre / señado / pagado / no_vino / cancelado
    monto_total REAL DEFAULT 0,
    monto_senia REAL DEFAULT 0,
    monto_pagado REAL DEFAULT 0,
    notas TEXT,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (cliente_id) REFERENCES clientes(id)
);

CREATE TABLE IF NOT EXISTS productos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    categoria TEXT DEFAULT 'otro',  -- bebida / minuta / snack / otro
    precio REAL NOT NULL,
    stock INTEGER DEFAULT 0,
    activo INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ventas_bufet (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,
    producto_id INTEGER,
    producto_nombre TEXT NOT NULL,
    cantidad INTEGER NOT NULL DEFAULT 1,
    precio_unitario REAL NOT NULL,
    total REAL NOT NULL,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS operadores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    apellido TEXT DEFAULT '',
    email TEXT UNIQUE NOT NULL,
    clave_hash TEXT NOT NULL,
    es_admin INTEGER DEFAULT 0,
    permisos TEXT DEFAULT '[]',
    activo INTEGER DEFAULT 1,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS configuracion (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL DEFAULT ''
);

-- Tickets del bufet (agrupa los ítems de cada venta)
CREATE TABLE IF NOT EXISTS tickets (
    numero INTEGER PRIMARY KEY AUTOINCREMENT,
    fecha TEXT NOT NULL,          -- YYYY-MM-DD
    hora TEXT NOT NULL,           -- HH:MM
    forma_pago TEXT DEFAULT 'efectivo',
    total REAL NOT NULL DEFAULT 0,
    efectivo_recibido REAL DEFAULT 0,
    notas TEXT DEFAULT '',
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Turnos fijos: el mismo cliente, mismo día de la semana, misma hora y cancha, todas las semanas
CREATE TABLE IF NOT EXISTS turnos_fijos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dia_semana INTEGER NOT NULL,  -- 0=lunes ... 6=domingo
    hora TEXT NOT NULL,
    cancha INTEGER NOT NULL,
    cliente_id INTEGER,
    cliente_nombre TEXT NOT NULL,
    monto_total REAL DEFAULT 0,
    desde TEXT NOT NULL,          -- YYYY-MM-DD primera fecha
    hasta TEXT,                   -- YYYY-MM-DD última fecha (NULL = sin fin)
    activo INTEGER DEFAULT 1,
    creado_en TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db


async def generar_turnos_fijos(db, desde: date, hasta: date) -> None:
    """Crea automáticamente (como 'señado') las reservas de los turnos fijos
    entre dos fechas, si todavía no existen. Si una semana se canceló ese turno
    fijo, no se vuelve a crear. Si otro cliente ocupa el horario, se respeta."""
    async with db.execute("SELECT * FROM turnos_fijos WHERE activo=1") as cur:
        fijos = await cur.fetchall()
    if not fijos:
        return
    creadas = False
    d = desde
    while d <= hasta:
        f = d.isoformat()
        for tf in fijos:
            if tf["dia_semana"] != d.weekday() or f < tf["desde"]:
                continue
            if tf["hasta"] and f > tf["hasta"]:
                continue
            async with db.execute(
                """SELECT id FROM reservas WHERE fecha=? AND hora=? AND cancha=?
                   AND (turno_fijo_id=? OR estado != 'cancelado') LIMIT 1""",
                (f, tf["hora"], tf["cancha"], tf["id"])
            ) as cur:
                if await cur.fetchone():
                    continue
            await db.execute(
                """INSERT INTO reservas (fecha, hora, cancha, cliente_id, cliente_nombre,
                   estado, monto_total, monto_senia, monto_pagado, notas, turno_fijo_id)
                   VALUES (?,?,?,?,?,'señado',?,0,0,'Turno fijo',?)""",
                (f, tf["hora"], tf["cancha"], tf["cliente_id"], tf["cliente_nombre"],
                 tf["monto_total"] or 0, tf["id"])
            )
            creadas = True
        d += timedelta(days=1)
    if creadas:
        await db.commit()

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
        # Migración: columna turno_fijo_id en reservas (bases ya existentes)
        async with db.execute("PRAGMA table_info(reservas)") as cur:
            columnas = [r[1] for r in await cur.fetchall()]
        if "turno_fijo_id" not in columnas:
            await db.execute("ALTER TABLE reservas ADD COLUMN turno_fijo_id INTEGER")
            await db.commit()
        # Migración: datos extra de productos (formato El Roble)
        async with db.execute("PRAGMA table_info(productos)") as cur:
            cols_prod = [r[1] for r in await cur.fetchall()]
        for col, tipo in [("precio_compra", "REAL DEFAULT 0"), ("proveedor", "TEXT DEFAULT ''"),
                          ("stock_minimo", "INTEGER DEFAULT 0"),
                          ("fecha_actualizacion", "TEXT DEFAULT ''"),
                          ("fecha_control_stock", "TEXT DEFAULT ''")]:
            if col not in cols_prod:
                await db.execute(f"ALTER TABLE productos ADD COLUMN {col} {tipo}")
        await db.commit()
        # Migración: cada ítem vendido apunta a su ticket
        async with db.execute("PRAGMA table_info(ventas_bufet)") as cur:
            cols_v = [r[1] for r in await cur.fetchall()]
        if "ticket_numero" not in cols_v:
            await db.execute("ALTER TABLE ventas_bufet ADD COLUMN ticket_numero INTEGER")
            await db.commit()
        # Migración: cuánto de cada ticket se cobró en efectivo y cuánto por transferencia
        async with db.execute("PRAGMA table_info(tickets)") as cur:
            cols_t = [r[1] for r in await cur.fetchall()]
        for col in ("monto_efectivo", "monto_transferencia"):
            if col not in cols_t:
                await db.execute(f"ALTER TABLE tickets ADD COLUMN {col} REAL DEFAULT 0")
        await db.commit()
        # Productos de ejemplo si la tabla está vacía
        async with db.execute("SELECT COUNT(*) FROM productos") as cur:
            count = (await cur.fetchone())[0]
        if count == 0:
            productos_iniciales = [
                ("Coca Cola 500ml", "bebida", 2000, 20),
                ("Agua Mineral", "bebida", 1200, 30),
                ("Gatorade", "bebida", 2500, 15),
                ("Cerveza 500ml", "bebida", 3500, 24),
                ("Milanesa en pan", "minuta", 5000, 0),
                ("Choripan", "minuta", 4500, 0),
                ("Lomito", "minuta", 7000, 0),
                ("Sandwich vegetal", "minuta", 4000, 0),
                ("Papas fritas", "snack", 3000, 0),
                ("Alfajor", "snack", 1500, 20),
                ("Maní", "snack", 1000, 30),
            ]
            await db.executemany(
                "INSERT INTO productos (nombre, categoria, precio, stock) VALUES (?,?,?,?)",
                productos_iniciales
            )
            await db.commit()
