import asyncio, json
from pathlib import Path
from .db import connect, put


async def main():
    db = await connect()
    data = json.loads(Path(__file__).with_name("catalog.json").read_text())
    for source in data["sources"]:
        await db.execute(
            "INSERT INTO sources(id,title,enabled,department) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            source["id"],
            source["title"],
            source["enabled"],
            source["department"],
        )
    for c in data["categories"]:
        await db.execute(
            "INSERT INTO categories(code,title,department,description,destination,link,single_only) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT DO NOTHING",
            c["code"],
            c["title"],
            c["department"],
            c["description"],
            c.get("destination"),
            c["link"],
            c.get("single_only",False),
        )
    for code, title, dept, desc in [
        ("accessories.women_shoes", "Женская обувь", "accessories", "Женская обувь"),
        ("accessories.men_shoes", "Мужская обувь", "accessories", "Мужская обувь"),
        ("accessories.bag", "Сумки", "accessories", "Сумки"),
        (
            "women.blazer",
            "Женские пиджаки",
            "women",
            "Отдельный пиджак / жакет, не комплект",
        ),
    ]:
        await db.execute(
            "INSERT INTO categories(code,title,department,description) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING",
            code,
            title,
            dept,
            desc,
        )
    for k, v in {
        "paused": True,
        "mode": "REVIEW",
        "auto_threshold": 0.85,
        "review_threshold": 0.60,
        "model": "gemini-3.1-flash-lite",
        "input_price": 0.25,
        "output_price": 1.50,
    }.items():
        await db.execute(
            "INSERT INTO settings VALUES($1,$2) ON CONFLICT DO NOTHING", k, v
        )
    await db.close()


asyncio.run(main())
