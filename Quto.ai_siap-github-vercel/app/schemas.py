import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+$")


def _validate_name(value: str) -> str:
    name = value.strip()
    if len(name) < 2:
        raise ValueError("Nama wajib diisi.")
    if len(name) > 100:
        raise ValueError("Nama terlalu panjang.")
    return name


def _validate_email(value: str) -> str:
    email = value.strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError("Format email tidak valid.")
    return email


class GenerateRequest(BaseModel):
    course_id: str
    material_id: str
    title: str
    start_at: datetime
    duration_minutes: int = Field(ge=5)
    count: Literal[5, 10, 15]
    type: Literal["mcq", "short", "mix"]
    difficulty: Literal["easy", "medium", "hard"]


class LoginRequest(BaseModel):
    email: str
    password: str
    role: Literal["lecturer", "student"] | None = None

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _validate_email(value)


class RegistrationFields(BaseModel):
    name: str = Field(max_length=100)
    email: str
    password: str = Field(min_length=6, max_length=100)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _validate_name(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _validate_email(value)


class RegisterRequest(RegistrationFields):
    role: Literal["lecturer", "student"] = "student"


class LecturerRegisterRequest(RegistrationFields):
    invitation_token: str = Field(min_length=32, max_length=200)


class LecturerInvitationRequest(BaseModel):
    email: str
    expires_in_hours: int = Field(default=72, ge=1, le=168)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _validate_email(value)


class ProfileUpdateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)
    email: str | None = None
    password: str | None = Field(default=None, min_length=6, max_length=100)
    current_password: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _validate_name(value)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str | None) -> str | None:
        return None if value is None else _validate_email(value)


class ResetPasswordRequest(BaseModel):
    """Lama — hanya email+password tanpa verifikasi (tidak dipakai lagi)."""
    email: str
    password: str = Field(min_length=6, max_length=100)

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _validate_email(value)


class ResetPasswordRequestInit(BaseModel):
    """Langkah 1: minta token reset untuk email tertentu."""
    email: str

    @field_validator("email")
    @classmethod
    def _email(cls, value: str) -> str:
        return _validate_email(value)


class ResetPasswordConfirm(BaseModel):
    """Langkah 2: gunakan token untuk menyetel password baru."""
    token: str
    password: str = Field(min_length=6, max_length=100)


class CourseCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = Field("", max_length=500)


class QuizCreateRequest(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    text: str = Field(min_length=50)
    count: int = Field(5, ge=1, le=15)
    type: Literal["mcq", "short", "mix"] = "mix"
    difficulty: Literal["easy", "medium", "hard"] = "medium"


class QuizSubmitRequest(BaseModel):
    answers: dict[str, str]
