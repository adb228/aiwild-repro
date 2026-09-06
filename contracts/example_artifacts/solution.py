from pathlib import Path


def main() -> None:
    Path("output.xlsx").touch()


if __name__ == "__main__":
    main()
