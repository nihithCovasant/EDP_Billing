import os
import random
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(".env.test")
load_dotenv()  # fallback to .env if .env.test isn't present

FILE_ROOT_PATH = os.getenv("FILE_ROOT_PATH", "./edp")
DATE_FOLDER_FORMAT = os.getenv("DATE_FOLDER_FORMAT", "%Y-%m-%d")

# segment -> how many files to generate directly under {segment}/{date}/, per date
SEGMENT_LAYOUT = {
    "EQ": 3,
    "FO": 2,
    "CUR": 1,
    "MCX": 1,
    "SLBM": 1,
}

CLIENT_CODES = [f"C{n:03d}" for n in range(1, 21)]


def get_processing_dates() -> list[str]:
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    return [today.strftime(DATE_FOLDER_FORMAT), yesterday.strftime(DATE_FOLDER_FORMAT)]


def _csv_content(trade_id_start: int, row_count: int) -> str:
    lines = ["TradeID,ClientCode,Quantity,Price"]
    for i in range(row_count):
        trade_id = trade_id_start + i
        client_code = random.choice(CLIENT_CODES)
        quantity = random.randint(1, 500)
        price = round(random.uniform(10.0, 5000.0), 2)
        lines.append(f"{trade_id},{client_code},{quantity},{price}")
    return "\n".join(lines) + "\n"


def generate_date_folder(root: Path, date_str: str) -> int:
    trade_id_seed = 1000
    files_created = 0

    for segment, num_files in SEGMENT_LAYOUT.items():
        date_dir = root / segment / date_str
        date_dir.mkdir(parents=True, exist_ok=True)

        for file_index in range(1, num_files + 1):
            file_name = f"trade_{file_index:03d}.csv"
            file_path = date_dir / file_name
            row_count = random.randint(3, 6)
            file_path.write_text(_csv_content(trade_id_seed, row_count), encoding="utf-8")

            print(f"Created: {file_path}")
            trade_id_seed += row_count
            files_created += 1

    return files_created


def main() -> None:
    root = Path(FILE_ROOT_PATH)
    root.mkdir(parents=True, exist_ok=True)

    dates = get_processing_dates()
    print(f"Generating test data under: {root.resolve()}")
    print(f"Dates: {dates}")

    total_files = 0
    for date_str in dates:
        created = generate_date_folder(root, date_str)
        print(f"  {date_str}: {created} files")
        total_files += created

    print(f"Done. Total files created: {total_files}")


if __name__ == "__main__":
    main()
