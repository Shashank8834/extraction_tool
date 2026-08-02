import uuid
from datetime import datetime

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from .database import Base

# JSONB on Postgres (production), portable JSON elsewhere (e.g. SQLite in tests).
JSONType = JSON().with_variant(JSONB, "postgresql")


def _uuid() -> str:
    return uuid.uuid4().hex


class SubmissionLink(Base):
    """A one-time intake link the admin generates and sends to a client."""

    __tablename__ = "submission_links"

    id = Column(String, primary_key=True, default=_uuid)
    token = Column(String, unique=True, index=True, nullable=False, default=_uuid)
    label = Column(String, nullable=False, default="")  # e.g. client name / reference
    status = Column(String, nullable=False, default="pending")  # pending | completed
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    submission = relationship(
        "Submission", back_populates="link", uselist=False, cascade="all, delete-orphan"
    )

    @property
    def effective_status(self) -> str:
        """pending | completed | expired — derived from status + expiry."""
        if self.status == "completed":
            return "completed"
        if self.expires_at and self.expires_at < datetime.utcnow():
            return "expired"
        return "pending"

    @property
    def is_open(self) -> bool:
        return self.effective_status == "pending"


class Submission(Base):
    """The data a client submitted through a link."""

    __tablename__ = "submissions"

    id = Column(String, primary_key=True, default=_uuid)
    # One submission per link, enforced at the DB level (guards against a
    # double-submit race on the same open link).
    link_id = Column(String, ForeignKey("submission_links.id"), nullable=False, unique=True)
    form_data = Column(JSONType, nullable=False, default=dict)  # {field_key: value}
    client_ip = Column(String, nullable=True)
    submitted_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    link = relationship("SubmissionLink", back_populates="submission")
    uploads = relationship(
        "Upload", back_populates="submission", cascade="all, delete-orphan"
    )


class Upload(Base):
    """A single uploaded document plus its OCR-extracted data."""

    __tablename__ = "uploads"

    id = Column(String, primary_key=True, default=_uuid)
    submission_id = Column(String, ForeignKey("submissions.id"), nullable=False)
    field_key = Column(String, nullable=False)
    doc_type = Column(String, nullable=False, default="generic")  # aadhaar | pan | generic
    original_filename = Column(String, nullable=False)
    stored_filename = Column(String, nullable=False)  # encrypted blob on disk
    content_type = Column(String, nullable=True)
    size_bytes = Column(Integer, nullable=True)
    raw_text = Column(Text, nullable=True)
    extracted_data = Column(JSONType, nullable=True)  # parsed fields, e.g. {"pan": "...", "name": "..."}

    submission = relationship("Submission", back_populates="uploads")
