# SPDX-FileCopyrightText: 2023 Marlon W (Mawoka)
#
# SPDX-License-Identifier: MPL-2.0


import base64
import hashlib
import json
import os
import random
import secrets

import socketio
from cryptography.fernet import Fernet

from classquiz.config import redis, settings
from classquiz.db.models import (
    PlayGame,
    QuizQuestionType,
    GameSession,
    GamePlayer,
    VotingQuizAnswer,
    AnswerDataList,
    AnswerData,
)
from pydantic import BaseModel, ValidationError
from datetime import datetime

from classquiz.socket_server.helpers import (
    check_answer,
    check_captcha,
    has_already_answered,
)
from .models import (
    RejoinGameData,
    JoinGameData,
    ReturnQuestion,
    SubmitAnswerData,
    RegisterAsAdminData,
    KickPlayerInput,
    ConnectSessionIdEvent,
    PlayerIdentityData,
)

from classquiz.socket_server.export_helpers import save_quiz_to_storage
from classquiz.socket_server.session import get_session, save_session

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins=[])
settings = settings()


def get_fernet_key() -> bytes:
    hlib = hashlib.sha256()
    hlib.update(settings.secret_key.encode("utf-8"))
    return base64.urlsafe_b64encode(hlib.hexdigest().encode("latin-1")[:32])


fernet = Fernet(get_fernet_key())


async def generate_final_results(game_data: PlayGame, game_pin: str) -> dict:
    results = {}
    for i in range(len(game_data.questions)):
        redis_res = await redis.get(f"game_session:{game_pin}:{i}")
        if redis_res is None:
            continue
        else:
            results[str(i)] = json.loads(redis_res)
    return results


def calculate_score(z: float, t: int) -> int:
    t = t * 1000
    res = (t - z) / t
    return int(res * 1000)


async def set_answer(answers, game_pin: str, q_index: int, data: AnswerData) -> AnswerDataList:
    if answers is None:
        answers = AnswerDataList([data])
    else:
        answers = AnswerDataList.model_validate_json(answers)
        answers.append(data)
    await redis.set(
        f"game_session:{game_pin}:{q_index}",
        answers.model_dump_json(),
        ex=7200,
    )
    return answers


def _player_token_key(game_pin: str, player_token: str) -> str:
    return f"game_session:{game_pin}:player_token:{player_token}"


def _player_token_of_username_key(game_pin: str, username: str) -> str:
    return f"game_session:{game_pin}:player_token_of:{username}"


async def _get_or_create_player_token(game_pin: str, username: str, provided_token: str | None) -> str:
    if provided_token:
        return provided_token
    existing = await redis.get(_player_token_of_username_key(game_pin, username))
    if existing:
        return existing
    return secrets.token_urlsafe(24)


async def _attach_player(
    sid: str,
    game_pin: str,
    username: str,
    player_token: str,
    admin: bool = False,
) -> None:
    session = {
        "game_pin": game_pin,
        "username": username,
        "sid_custom": sid,
        "admin": admin,
        "player_token": player_token,
        # default until time_sync roundtrip happens
        "ping": 0,
    }
    await save_session(sid, sio, session)
    await sio.enter_room(sid, game_pin)


async def _send_player_state(sid: str, game_data: PlayGame) -> None:
    session = await get_session(sid, sio, disconnect_on_error=False)
    await sio.emit("joined_game", game_data.to_player_data(), room=sid)
    if game_data.started:
        await sio.emit("start_game", room=sid)
    if game_data.question_show:
        q_i = int(float(game_data.current_question))
        if game_data.questions[q_i].type == QuizQuestionType.SLIDE:
            await sio.emit(
                "set_question_number",
                {"question_index": q_i},
                room=sid,
            )
            return
        temp_return = game_data.model_dump(include={"questions"})["questions"][q_i]
        if game_data.questions[q_i].type == QuizQuestionType.VOTING:
            for i in range(len(temp_return["answers"])):
                temp_return["answers"][i] = VotingQuizAnswer(**temp_return["answers"][i])
        if game_data.questions[q_i].type in (QuizQuestionType.ABCD, QuizQuestionType.CHECK):
            cleaned = []
            for a in temp_return["answers"]:
                cleaned.append({"answer": a.get("answer"), "color": a.get("color")})
            temp_return["answers"] = cleaned
        temp_return["type"] = game_data.questions[q_i].type
        if temp_return["type"] == QuizQuestionType.ORDER:
            random.shuffle(temp_return["answers"])
        started_at = None
        if session and session.get("game_pin"):
            started_at = await redis.get(f"game:{session['game_pin']}:current_time")
        await sio.emit(
            "set_question_number",
            {
                "question_index": q_i,
                "question": temp_return,
                "started_at": started_at,
            },
            room=sid,
        )


@sio.event
async def rejoin_game(sid: str, data: dict):
    redis_res = await redis.get(f"game:{data['game_pin']}")
    if redis_res is None:
        await sio.emit("game_not_found", room=sid)
        return
    try:
        data = RejoinGameData(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return
    identity_raw = await redis.get(_player_token_key(data.game_pin, data.player_token))
    if identity_raw is None:
        await sio.emit("rejoin_failed", {"reason": "unknown_player"}, room=sid)
        return
    identity = PlayerIdentityData.model_validate_json(identity_raw)
    encrypted_datetime = fernet.encrypt(datetime.now().isoformat().encode("utf-8")).decode("utf-8")
    await sio.emit("time_sync", encrypted_datetime, room=sid)
    redis_sid_key = f"game_session:{data.game_pin}:players:{identity.username}"
    old_sid = await redis.get(redis_sid_key)
    if old_sid:
        await redis.srem(
            f"game_session:{data.game_pin}:players",
            GamePlayer(username=identity.username, sid=old_sid).model_dump_json(),
        )
    await redis.set(redis_sid_key, sid, ex=7200)
    await redis.sadd(
        f"game_session:{data.game_pin}:players",
        GamePlayer(username=identity.username, sid=sid).model_dump_json(),
    )
    game_data = PlayGame.model_validate_json(redis_res)
    await _attach_player(
        sid=sid,
        game_pin=data.game_pin,
        username=identity.username,
        player_token=identity.player_token,
        admin=False,
    )
    await sio.emit(
        "rejoined_game",
        game_data.to_player_data(),
        room=sid,
    )
    await _send_player_state(sid, game_data)


@sio.event
async def join_game(sid: str, data: dict):
    redis_res = await redis.get(f"game:{data['game_pin']}")
    if redis_res is None:
        await sio.emit("game_not_found", room=sid)
        return
    try:
        data = JoinGameData(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return
    game_data = PlayGame.model_validate_json(redis_res)
    # +++ START checking captcha +++
    if game_data.captcha_enabled:
        captcha_res = await check_captcha(data.captcha)
        if not captcha_res:
            await sio.emit("captcha_failed", room=sid)
            return
    # --- END checking captcha ---
    existing_sid = await redis.get(f"game_session:{data.game_pin}:players:{data.username}")
    if existing_sid is not None:
        existing_token = await redis.get(_player_token_of_username_key(data.game_pin, data.username))
        if existing_token:
            await sio.emit(
                "username_already_exists",
                {"player_token": existing_token},
                room=sid,
            )
            return
        await sio.emit("username_already_exists", room=sid)
        return

    player_token = await _get_or_create_player_token(data.game_pin, data.username, data.player_token)
    identity = PlayerIdentityData(username=data.username, player_token=player_token)
    await redis.set(_player_token_key(data.game_pin, player_token), identity.model_dump_json(), ex=7200)
    await redis.set(_player_token_of_username_key(data.game_pin, data.username), player_token, ex=7200)

    await _attach_player(
        sid=sid,
        game_pin=data.game_pin,
        username=data.username,
        player_token=player_token,
        admin=False,
    )
    await sio.emit(
        "player_identity",
        {"player_token": player_token, "username": data.username, "game_pin": data.game_pin},
        room=sid,
    )
    await _send_player_state(sid, game_data)
    await redis.set(f"game_session:{data.game_pin}:players:{data.username}", sid, ex=7200)
    await GamePlayer(username=data.username, sid=sid).to_player_stack(data.game_pin)

    if data.custom_field == "":
        data.custom_field = None
    if data.custom_field is not None:
        await redis.hset(
            f"game:{data.game_pin}:players:custom_fields",
            data.username,
            data.custom_field,
        )

    await sio.emit(
        "player_joined",
        {"username": data.username, "sid": sid},
        room=f"admin:{data.game_pin}",
    )
    # +++ Time-Sync +++
    encrypted_datetime = fernet.encrypt(datetime.now().isoformat().encode("utf-8")).decode("utf-8")
    await sio.emit("time_sync", encrypted_datetime, room=sid)
    # --- Time-Sync ---
    await sio.enter_room(sid, data.game_pin)


@sio.event
async def start_game(sid: str, _data: dict):
    session = await get_session(sid, sio)
    if not session["admin"]:
        return
    game_data = await PlayGame.get_from_redis(session["game_pin"])
    game_data.started = True
    await game_data.save(session["game_pin"])
    await redis.delete(f"game_in_lobby:{game_data.user_id.hex}")
    await sio.emit("start_game", room=session["game_pin"])


@sio.event
async def register_as_admin(sid: str, data: dict):
    try:
        data = RegisterAsAdminData(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return
    game_pin = data.game_pin
    game_id = data.game_id
    if await redis.get(f"game_session:{game_pin}") is not None:
        await sio.emit("already_registered_as_admin", room=sid)
        return
    await GameSession(admin=sid, game_id=game_id, answers=[]).save(game_pin)
    await sio.emit(
        "registered_as_admin",
        {"game_id": game_id, "game": await redis.get(f"game:{game_pin}")},
        room=sid,
    )
    session = {"game_pin": game_pin, "admin": True, "remote": False}
    await save_session(sid, sio, session)
    await sio.enter_room(sid, game_pin)
    await sio.enter_room(sid, f"admin:{data.game_pin}")


@sio.event
async def get_question_results(sid: str, data: dict):
    session = await get_session(sid, sio)
    if not session["admin"]:
        return
    game_pin = session["game_pin"]
    answer_data_list = await AnswerDataList.get_redis_or_empty(game_pin, data["question_number"])
    game_data = await PlayGame.get_from_redis(game_pin)
    game_data.question_show = False
    await game_data.save(game_pin)
    await sio.emit("question_results", answer_data_list.model_dump(), room=game_pin)


@sio.event
async def set_question_number(sid: str, data: str):
    # data is just a number (as a str) of the question
    session = await get_session(sid, sio)
    if not session["admin"]:
        return
    game_pin = session["game_pin"]
    game_data = await PlayGame.get_from_redis(session["game_pin"])
    game_data.current_question = int(float(data))
    game_data.question_show = True
    await game_data.save(session["game_pin"])
    await redis.set(f"game:{session['game_pin']}:current_time", datetime.now().isoformat(), ex=7200)
    started_at = await redis.get(f"game:{session['game_pin']}:current_time")
    temp_return = game_data.model_dump(include={"questions"})["questions"][int(float(data))]
    if game_data.questions[int(float(data))].type == QuizQuestionType.SLIDE:
        await sio.emit(
            "set_question_number",
            {
                "question_index": int(float(data)),
            },
            room=sid,
        )
        return
    if game_data.questions[int(float(data))].type == QuizQuestionType.VOTING:
        for i in range(len(temp_return["answers"])):
            temp_return["answers"][i] = VotingQuizAnswer(**temp_return["answers"][i])
    if game_data.questions[int(float(data))].type in (QuizQuestionType.ABCD, QuizQuestionType.CHECK):
        # Strip solution information before sending to players.
        # `ReturnQuestion` expects `ABCDQuizAnswerWithoutSolution` entries.
        cleaned = []
        for a in temp_return["answers"]:
            cleaned.append({"answer": a.get("answer"), "color": a.get("color")})
        temp_return["answers"] = cleaned
    temp_return["type"] = game_data.questions[int(float(data))].type
    if temp_return["type"] == QuizQuestionType.ORDER:
        random.shuffle(temp_return["answers"])
    await sio.emit(
        "set_question_number",
        {
            "question_index": int(float(data)),
            "question": temp_return,
            "started_at": started_at,
        },
        room=game_pin,
    )


@sio.event
async def submit_answer(sid: str, data: dict):
    now = datetime.now()
    try:
        data = SubmitAnswerData(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return
    data.answer = str(data.answer)
    session = await get_session(sid, sio)
    question_index = int(float(data.question_index))
    game_data = await PlayGame.get_from_redis(session["game_pin"])
    already_answered = await has_already_answered(session["game_pin"], question_index, session["username"])
    if already_answered:
        await sio.emit("already_replied", room=sid)
        return
    answer_right, answer = check_answer(game_data, data)
    latency = int(float(session.get("ping", 0) or 0))
    started_raw = await redis.get(f"game:{session['game_pin']}:current_time")
    if started_raw is None:
        # Fallback: treat question as starting now if timestamp missing.
        started_raw = datetime.now().isoformat()
    time_q_started = datetime.fromisoformat(started_raw)
    diff = (now - time_q_started).total_seconds() * 1000
    score = 0
    if answer_right:
        score = calculate_score(
            abs(diff) - latency,
            int(float(game_data.questions[question_index].time)),
        )
        if score > 1000:
            score = 1000
    await redis.hincrby(f"game_session:{session['game_pin']}:player_scores", session["username"], score)
    answer_data = AnswerData(
        username=session["username"],
        answer=answer,
        right=answer_right,
        time_taken=abs(diff) - latency,
        score=score,
    )
    answers = await redis.get(f"game_session:{session['game_pin']}:{data.question_index}")
    answers = await set_answer(
        answers,
        game_pin=session["game_pin"],
        data=answer_data,
        q_index=int(float(data.question_index)),
    )
    player_count = await redis.scard(f"game_session:{session['game_pin']}:players")
    await sio.emit("player_answer", {})
    if len(answers) == player_count:
        game_data = await PlayGame.get_from_redis(session["game_pin"])
        game_data.question_show = False
        await game_data.save(session["game_pin"])
        await sio.emit("everyone_answered", {})


@sio.event
async def get_final_results(sid: str, _data: dict):
    session: dict = await get_session(sid, sio)
    if not session["admin"]:
        return
    game_data = await PlayGame.get_from_redis(session["game_pin"])
    results = await generate_final_results(game_data, session["game_pin"])
    await sio.emit("final_results", results, room=session["game_pin"])


@sio.event
async def get_export_token(sid: str):
    session = await get_session(sid, sio)
    if not session["admin"]:
        return
    game_data = await PlayGame.get_from_redis(session["game_pin"])
    results = await generate_final_results(game_data, session["game_pin"])
    token = os.urandom(32).hex()
    await redis.set(f"export_token:{token}", json.dumps(results), ex=7200)
    await sio.emit("export_token", token, room=sid)


@sio.event
async def show_solutions(sid: str, _data: dict):
    session: dict = await get_session(sid, sio)
    game_data = await PlayGame.get_from_redis(session["game_pin"])
    if not session["admin"]:
        return
    await sio.emit(
        "solutions",
        game_data.questions[game_data.current_question].model_dump(),
        room=session["game_pin"],
    )


@sio.event
async def echo_time_sync(sid: str, data: str):
    then_dec = fernet.decrypt(data).decode("utf-8")
    then = datetime.fromisoformat(then_dec)
    now = datetime.now()
    delta = now - then
    session = await get_session(sid, sio)
    session["ping"] = delta.microseconds / 1000
    await save_session(sid, sio, session)


@sio.event
async def kick_player(sid: str, data: dict):
    try:
        data = KickPlayerInput(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return

    session: dict = await get_session(sid, sio)
    if not session["admin"]:
        return

    player_sid = await redis.get(f"game_session:{session['game_pin']}:players:{data.username}")
    await redis.srem(
        f"game_session:{session['game_pin']}:players",
        GamePlayer(username=data.username, sid=player_sid).model_dump_json(),
    )
    await sio.leave_room(player_sid, session["game_pin"])
    await sio.emit("kick", room=player_sid)


class _RegisterAsRemoteInput(BaseModel):
    game_pin: str
    game_id: str


@sio.event
async def register_as_remote(sid: str, data: dict):
    try:
        data = _RegisterAsRemoteInput(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return
    await sio.emit(
        "registered_as_admin",
        {"game_id": data.game_id, "game": await redis.get(f"game:{data.game_pin}")},
        room=sid,
    )
    await sio.emit("control_visibility", {"visible": False}, room=f"admin:{data.game_pin}")
    session = await get_session(sid, sio)
    session["game_pin"] = data.game_pin
    session["admin"] = True
    session["remote"] = True
    await save_session(sid, sio, session)
    await sio.enter_room(sid, data.game_pin)
    await sio.enter_room(sid, f"admin:{data.game_pin}")


class _SetControlVisibilityInput(BaseModel):
    visible: bool


@sio.event
async def set_control_visibility(sid: str, data: dict):
    try:
        data = _SetControlVisibilityInput(**data)
    except ValidationError as e:
        await sio.emit("error", room=sid)
        print(e)
        return
    session: dict = await get_session(sid, sio)
    await sio.emit(
        "control_visibility",
        {"visible": data.visible},
        room=f"admin:{session['game_pin']}",
    )


@sio.event
async def save_quiz(sid: str):
    session: dict = await get_session(sid, sio)
    if not session["admin"]:
        return
    await save_quiz_to_storage(session["game_pin"])
    await sio.emit("results_saved_successfully")


@sio.event
async def connect(sid: str, _environ, _auth):
    session_id = os.urandom(16).hex()
    print("Connection opened with handler")
    sio_session = {"session_id": session_id}
    await sio.save_session(sid, sio_session)
    await sio.emit("session_id", ConnectSessionIdEvent(session_id=session_id).dict())
