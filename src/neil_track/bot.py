from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
import discord
from curl_cffi import requests as curl_requests
from dotenv import load_dotenv

LOG = logging.getLogger("neil_track")
DEFAULT_LIFE360_BASE_URL = "https://api-cloudfront.life360.com/v3"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
SEATTLE = (47.6062, -122.3321)
AUSTIN = (30.2672, -97.7431)


@dataclass(frozen=True)
class Config:
    discord_bot_token: str
    discord_channel_id: int
    life360_username: str
    life360_password: str
    life360_client_basic: str
    life360_access_token: str | None
    life360_base_url: str
    life360_impersonate: str
    life360_member_id: str | None
    life360_member_name: str
    life360_circle_id: str | None
    poll_seconds: int
    state_file: Path
    announce_on_startup: bool
    nominatim_user_agent: str
    discord_message_template: str


@dataclass(frozen=True)
class Location:
    latitude: float
    longitude: float


@dataclass(frozen=True)
class Place:
    key: str
    city: str
    state: str
    country: str

    @property
    def label(self) -> str:
        parts = [self.city, self.state, self.country]
        return ", ".join(part for part in parts if part)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(dotenv_path=Path(".env"))
    config = load_config()
    bot = NeilTrackBot(config)
    bot.run(config.discord_bot_token)


def load_config() -> Config:
    life360_access_token = optional_env("LIFE360_ACCESS_TOKEN")
    life360_password = optional_env("LIFE360_PASSWORD")
    life360_client_basic = optional_env("LIFE360_CLIENT_BASIC")
    if not life360_access_token and not (life360_password and life360_client_basic):
        raise RuntimeError(
            "Missing Life360 auth. Set LIFE360_ACCESS_TOKEN in .env, or set both "
            "LIFE360_PASSWORD and LIFE360_CLIENT_BASIC for the fallback login flow."
        )

    return Config(
        discord_bot_token=require_env("DISCORD_BOT_TOKEN"),
        discord_channel_id=int(require_env("DISCORD_CHANNEL_ID")),
        life360_username=require_env("LIFE360_USERNAME"),
        life360_password=life360_password or "",
        life360_client_basic=life360_client_basic or "",
        life360_access_token=life360_access_token,
        life360_base_url=os.getenv("LIFE360_BASE_URL", DEFAULT_LIFE360_BASE_URL).rstrip("/"),
        life360_impersonate=os.getenv("LIFE360_IMPERSONATE", "chrome124"),
        life360_member_id=optional_env("LIFE360_MEMBER_ID"),
        life360_member_name=os.getenv("LIFE360_MEMBER_NAME", "Neil"),
        life360_circle_id=optional_env("LIFE360_CIRCLE_ID"),
        poll_seconds=int(os.getenv("POLL_SECONDS", "300")),
        state_file=Path(os.getenv("STATE_FILE", "state.json")),
        announce_on_startup=parse_bool(os.getenv("ANNOUNCE_ON_STARTUP", "false")),
        nominatim_user_agent=os.getenv("NOMINATIM_USER_AGENT", "neil-track-discord-bot/1.0"),
        discord_message_template=os.getenv(
            "DISCORD_MESSAGE_TEMPLATE",
            "{name} just arrived in {place}. He is {route_percent}% of the way from Seattle to Austin.",
        ),
    )


def require_env(name: str) -> str:
    value = optional_env(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    stripped = value.strip()
    if stripped.startswith("replace-with-"):
        return None
    return stripped


def parse_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


class NeilTrackBot(discord.Client):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.config = config
        self.poll_task: asyncio.Task[None] | None = None

    async def setup_hook(self) -> None:
        self.poll_task = asyncio.create_task(self.poll_loop(), name="life360-poll-loop")

    async def close(self) -> None:
        if self.poll_task:
            self.poll_task.cancel()
        await super().close()

    async def on_ready(self) -> None:
        LOG.info("Logged in to Discord as %s", self.user)

    async def poll_loop(self) -> None:
        await self.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            life360 = Life360Client(session, self.config)
            geocoder = ReverseGeocoder(session, self.config)
            while not self.is_closed():
                try:
                    await self.check_location(life360, geocoder)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOG.exception("Location check failed")
                await asyncio.sleep(self.config.poll_seconds)

    async def check_location(self, life360: "Life360Client", geocoder: "ReverseGeocoder") -> None:
        member = await life360.find_member()
        location = extract_location(member)
        place = await geocoder.reverse(location)
        state = read_state(self.config.state_file)

        if state.get("last_place_key") == place.key:
            LOG.info("Neil is still in %s", place.label)
            return

        should_announce = bool(state) or self.config.announce_on_startup
        write_state(
            self.config.state_file,
            {
                "last_place_key": place.key,
                "last_place": place.label,
                "last_latitude": location.latitude,
                "last_longitude": location.longitude,
            },
        )

        if not should_announce:
            LOG.info("Recorded initial place without announcing: %s", place.label)
            return

        channel = self.get_channel(self.config.discord_channel_id)
        if channel is None:
            channel = await self.fetch_channel(self.config.discord_channel_id)
        if not isinstance(channel, (discord.TextChannel, discord.Thread, discord.DMChannel)):
            raise RuntimeError(f"Configured Discord channel is not messageable: {channel!r}")

        route_percent = seattle_to_austin_percent(location)
        message = self.config.discord_message_template.format(
            name=display_name(member, self.config.life360_member_name),
            city=place.city,
            state=place.state,
            country=place.country,
            place=place.label,
            lat=f"{location.latitude:.5f}",
            lon=f"{location.longitude:.5f}",
            route_percent=f"{route_percent:.1f}",
            route_percent_rounded=str(round(route_percent)),
        )
        await channel.send(message)
        LOG.info("Announced new place: %s", place.label)


class Life360Client:
    def __init__(self, session: aiohttp.ClientSession, config: Config) -> None:
        self.config = config
        self.access_token: str | None = config.life360_access_token

    async def find_member(self) -> dict[str, Any]:
        token = await self.get_access_token()
        headers = self.api_headers(token)
        circle_ids = [self.config.life360_circle_id] if self.config.life360_circle_id else await self.circle_ids(headers)

        for circle_id in circle_ids:
            response = await self.get(f"{self.config.life360_base_url}/circles/{circle_id}/members", headers)
            if response.status_code == 401 and self.can_refresh_token():
                self.access_token = None
                return await self.find_member()
            response.raise_for_status()
            payload = response.json()

            members = payload.get("members", payload if isinstance(payload, list) else [])
            for member in members:
                if self.is_target_member(member):
                    return member

        raise RuntimeError("Could not find configured Life360 member in available circles")

    async def circle_ids(self, headers: dict[str, str]) -> list[str]:
        response = await self.get(f"{self.config.life360_base_url}/circles", headers)
        response.raise_for_status()
        payload = response.json()
        circles = payload.get("circles", payload if isinstance(payload, list) else [])
        circle_ids = [str(circle["id"]) for circle in circles if circle.get("id")]
        if not circle_ids:
            raise RuntimeError("Life360 returned no circles")
        return circle_ids

    async def get_access_token(self) -> str:
        if self.access_token:
            return self.access_token

        headers = {
            "Accept": "application/json",
            "Authorization": f"Basic {self.config.life360_client_basic}",
        }
        data = {
            "grant_type": "password",
            "username": self.config.life360_username,
            "password": self.config.life360_password,
        }
        response = await self.post(f"{self.config.life360_base_url}/oauth2/token", headers, data)
        response.raise_for_status()
        payload = response.json()

        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Life360 auth response did not include access_token")
        self.access_token = str(token)
        return self.access_token

    def can_refresh_token(self) -> bool:
        return bool(self.config.life360_password and self.config.life360_client_basic)

    def api_headers(self, token: str) -> dict[str, str]:
        headers = {
            "Accept": "*/*",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Origin": "https://www.life360.com",
            "Referer": "https://www.life360.com/",
            "ce-id": "226ed96f-43ae-464e-aff3-52e4ec4f49f2",
            "ce-source": "/web/firefox/142.0",
            "ce-version": "1.0",
        }
        if self.config.life360_circle_id:
            headers["circleid"] = self.config.life360_circle_id
        return headers

    async def get(self, url: str, headers: dict[str, str]) -> curl_requests.Response:
        return await asyncio.to_thread(
            curl_requests.get,
            url,
            headers=headers,
            impersonate=self.config.life360_impersonate,
            timeout=30,
        )

    async def post(self, url: str, headers: dict[str, str], data: dict[str, str]) -> curl_requests.Response:
        return await asyncio.to_thread(
            curl_requests.post,
            url,
            headers=headers,
            data=data,
            impersonate=self.config.life360_impersonate,
            timeout=30,
        )

    def is_target_member(self, member: dict[str, Any]) -> bool:
        if self.config.life360_member_id and str(member.get("id")) == self.config.life360_member_id:
            return True

        wanted = self.config.life360_member_name.casefold()
        names = [
            member.get("name"),
            member.get("firstName"),
            " ".join(str(part) for part in [member.get("firstName"), member.get("lastName")] if part),
        ]
        return any(str(name).casefold() == wanted for name in names if name)


class ReverseGeocoder:
    def __init__(self, session: aiohttp.ClientSession, config: Config) -> None:
        self.session = session
        self.config = config

    async def reverse(self, location: Location) -> Place:
        params = {
            "format": "jsonv2",
            "lat": str(location.latitude),
            "lon": str(location.longitude),
            "zoom": "10",
            "addressdetails": "1",
        }
        headers = {"User-Agent": self.config.nominatim_user_agent}
        async with self.session.get(NOMINATIM_REVERSE_URL, params=params, headers=headers) as response:
            response.raise_for_status()
            payload = await response.json()

        address = payload.get("address", {})
        city = first_present(address, "city", "town", "village", "municipality", "hamlet", "county")
        state = first_present(address, "state", "region", "province")
        country = first_present(address, "country")
        if not city:
            city = payload.get("name") or "Unknown location"

        key = "|".join([normalize_place_part(city), normalize_place_part(state), normalize_place_part(country)])
        return Place(key=key, city=city, state=state, country=country)


def extract_location(member: dict[str, Any]) -> Location:
    location = member.get("location")
    if not isinstance(location, dict):
        raise RuntimeError("Life360 member response did not include a location object")

    latitude = location.get("latitude") or location.get("lat")
    longitude = location.get("longitude") or location.get("lon") or location.get("lng")
    if latitude is None or longitude is None:
        raise RuntimeError("Life360 member location did not include latitude and longitude")

    return Location(latitude=float(latitude), longitude=float(longitude))


def display_name(member: dict[str, Any], fallback: str) -> str:
    return str(member.get("name") or member.get("firstName") or fallback)


def first_present(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value)
    return ""


def normalize_place_part(value: str) -> str:
    return " ".join(value.casefold().split())


def seattle_to_austin_percent(location: Location) -> float:
    start_x, start_y = projected_route_point(*SEATTLE)
    end_x, end_y = projected_route_point(*AUSTIN)
    location_x, location_y = projected_route_point(location.latitude, location.longitude)

    route_x = end_x - start_x
    route_y = end_y - start_y
    route_length_squared = route_x * route_x + route_y * route_y
    if route_length_squared == 0:
        return 0.0

    location_x -= start_x
    location_y -= start_y
    progress = (location_x * route_x + location_y * route_y) / route_length_squared
    return max(0.0, min(100.0, progress * 100.0))


def projected_route_point(latitude: float, longitude: float) -> tuple[float, float]:
    mean_latitude = math.radians((SEATTLE[0] + AUSTIN[0]) / 2)
    return longitude * math.cos(mean_latitude), latitude


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
