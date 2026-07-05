from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, UniqueConstraint

from app.core.database import Base


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    __table_args__ = (UniqueConstraint("file_path", name="uq_uploaded_files_file_path"),)

    id = Column(Integer, primary_key=True)
    file_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    folder_date = Column(String, nullable=False)
    segment = Column(String, nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending | uploaded | failed
    cbos_response = Column(String, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    cbos_upload_id = Column(String, nullable=True)  # UUID CBOS returns on successful upload
    uploaded_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
