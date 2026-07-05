from pathlib import Path

from sqlalchemy.orm import Session

from app.models import UploadedFile


class UploadedFileRepository:
    """Data-access layer for the uploaded_files table. Callers own the
    Session lifecycle (create it, commit/close it) - this class only knows
    how to query and mutate rows through the session it's given."""

    def __init__(self, session: Session):
        self.session = session

    def get_by_id(self, record_id: int) -> UploadedFile | None:
        return self.session.query(UploadedFile).filter_by(id=record_id).first()

    def get_by_path(self, file_path) -> UploadedFile | None:
        return self.session.query(UploadedFile).filter_by(file_path=str(file_path)).first()

    def get_pending(self) -> list[UploadedFile]:
        return self.session.query(UploadedFile).filter_by(status="pending").all()

    def get_failed(self) -> list[UploadedFile]:
        return self.session.query(UploadedFile).filter_by(status="failed").all()

    def is_uploaded(self, file_path) -> bool:
        return (
            self.session.query(UploadedFile)
            .filter_by(file_path=str(file_path), status="uploaded")
            .first()
            is not None
        )

    def insert(self, **fields) -> UploadedFile:
        record = UploadedFile(**fields)
        self.session.add(record)
        self.session.flush()
        return record

    def get_or_create(self, file_path, folder_date: str, segment: str) -> UploadedFile:
        record = self.get_by_path(file_path)
        if record is None:
            record = self.insert(
                file_name=Path(file_path).name,
                file_path=str(file_path),
                folder_date=folder_date,
                segment=segment,
                status="pending",
            )
        return record

    def upsert_pending(self, file_path, folder_date: str, segment: str) -> UploadedFile:
        """Create a new row as pending, or reset an existing one back to
        pending. Used by the manual /upload endpoint."""
        record = self.get_by_path(file_path)
        if record is None:
            record = self.insert(
                file_name=Path(file_path).name,
                file_path=str(file_path),
                folder_date=folder_date,
                segment=segment,
                status="pending",
            )
        else:
            record.status = "pending"
            record.cbos_response = None
            record.uploaded_at = None

        self.commit()
        self.session.refresh(record)
        return record

    def update(self, record: UploadedFile, **fields) -> UploadedFile:
        for key, value in fields.items():
            setattr(record, key, value)
        return record

    def commit(self) -> None:
        self.session.commit()
