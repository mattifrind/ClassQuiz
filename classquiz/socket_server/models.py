# SPDX-FileCopyrightText: 2025 Marlon W (Mawoka)
#
# SPDX-License-Identifier: MPL-2.0

from pydantic import BaseModel, field_validator, ValidationInfo
from classquiz.db.models import QuizQuestion, QuizQuestionType, VotingQuizAnswer


class JoinGameData(BaseModel):
    username: str
    game_pin: str
    captcha: str | None = None
    custom_field: str | None = None
    player_token: str | None = None


class RejoinGameData(BaseModel):
    game_pin: str
    player_token: str


class PlayerIdentityData(BaseModel):
    username: str
    player_token: str


class RegisterAsAdminData(BaseModel):
    game_pin: str
    game_id: str


class ABCDQuizAnswerWithoutSolution(BaseModel):
    answer: str
    color: str | None = None


class RangeQuizAnswerWithoutSolution(BaseModel):
    min: int
    max: int


class ReturnQuestion(QuizQuestion):
    # Keep base field types to avoid incompatible override warnings.
    # Validation ensures the shape is safe for players.
    type: QuizQuestionType | None = QuizQuestionType.ABCD

    # Note: We intentionally do not add a custom `answers` validator here.
    # Player-safe shaping (e.g. stripping `right`) is done in the socket handlers.


class SubmitAnswerDataOrderType(BaseModel):
    answer: str


class SubmitAnswerData(BaseModel):
    question_index: int
    answer: str | int
    complex_answer: list[SubmitAnswerDataOrderType] | None = None


class KickPlayerInput(BaseModel):
    username: str


class ConnectSessionIdEvent(BaseModel):
    session_id: str
