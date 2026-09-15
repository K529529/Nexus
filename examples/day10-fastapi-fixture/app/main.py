from fastapi import FastAPI, HTTPException

app = FastAPI(title="Nexus Day 10 fixture")


def discount_percent(*, premium: bool, order_total: int) -> int:
    if order_total < 0:
        raise ValueError("order_total must be non-negative")
    if premium and order_total >= 100:
        return 5
    return 0


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/discount")
def discount(premium: bool = False, order_total: int = 0) -> dict[str, int]:
    try:
        percent = discount_percent(premium=premium, order_total=order_total)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"percent": percent}
