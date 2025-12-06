import polars as pl

def greet(who: str = "Quarto") -> str:
    return f"Hello, {who}! Welcome to {{ graft_name }}."

def show_dataframe() -> pl.DataFrame:
    return pl.DataFrame([1,2,3])