import typer

app = typer.Typer(name="symbiote", help="Symbiote — Kernel for persistent cognitive entities")


@app.callback()
def main() -> None:
    """Symbiote CLI."""


if __name__ == "__main__":
    app()
