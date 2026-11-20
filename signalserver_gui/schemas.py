"""Pydantic request/response models."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# Stations -------------------------------------------------------------------
class StationIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    latitude: float = Field(ge=-70, le=70, description="Signal-Server supports +/-70 degrees")
    longitude: float = Field(ge=-180, le=180)
    height_m: float = Field(10.0, ge=0, le=10000, description="Antenna height above ground")
    polarization: Literal["vertical", "horizontal"] = "vertical"
    rotation_deg: float = Field(0.0, ge=0, lt=360)
    downtilt_deg: float = Field(0.0, ge=-10, le=90)
    downtilt_direction_deg: float = Field(0.0, ge=0, lt=360)
    notes: str = ""


class StationOut(StationIn, ORM):
    id: int
    created_at: datetime
    updated_at: datetime


# Antennas -------------------------------------------------------------------
class AntennaUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    rx_gain_dbd: float | None = None
    description: str | None = None


class AntennaOut(ORM):
    id: int
    name: str
    category: str
    pattern: str
    rx_gain_dbd: float
    description: str
    created_at: datetime


# Plots ----------------------------------------------------------------------
class PlotIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    antenna_id: int
    tx_station_id: int
    rx_station_id: int | None = None
    frequency_mhz: float = Field(ge=20, le=100000)
    erp_watts: float = Field(1.0, ge=0, le=10_000_000)
    radius_km: float = Field(10.0, gt=0, le=300)
    resolution: Literal[300, 600, 1200] = 1200
    rx_height_m: float = Field(1.5, ge=0, le=1000)
    rx_threshold: float | None = None
    propagation_model: int = Field(1, ge=1, le=12)
    propagation_mode: int | None = Field(None, ge=1, le=3)
    terrain_code: int | None = Field(None, ge=1, le=6)
    terrain_dielectric: float | None = Field(None, ge=2, le=80)
    terrain_conductivity: float | None = Field(None, ge=0.0001, le=0.01)
    climate_code: int | None = Field(None, ge=1, le=7)
    reliability: int | None = Field(None, ge=1, le=99)
    confidence: int | None = Field(None, ge=1, le=99)
    ground_clutter_m: float | None = Field(None, ge=0, le=500)
    knife_edge: bool = False
    use_dbm: bool = True
    metric_units: bool = True
    do_p2p_analysis: bool = False
    color_palette: str = "rainbow"
    opacity: float = Field(0.7, ge=0, le=1)

    @model_validator(mode="after")
    def _check(self):
        if self.rx_station_id is not None and self.rx_station_id == self.tx_station_id:
            raise ValueError("Transmitter and receiver stations must be different")
        if self.do_p2p_analysis and self.rx_station_id is None:
            raise ValueError("Path analysis needs a receiver station")
        return self


class PlotOut(PlotIn, ORM):
    id: int
    created_at: datetime
    updated_at: datetime
    latest_run: "RunSummary | None" = None


# Runs / jobs ----------------------------------------------------------------
class JobOut(ORM):
    id: int
    kind: str
    status: str
    stage: str
    progress: float
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RunSummary(ORM):
    id: int
    plot_id: int
    plot_name: str | None = None
    status: str
    stage: str = ""
    progress: float = 0.0
    error: str | None = None
    north: float | None
    south: float | None
    east: float | None
    west: float | None
    created_at: datetime
    finished_at: datetime | None
    files: list[str] = []
    legend: list[dict] = []
    image_url: str | None = None
    opacity: float = 0.7
    units: str = "dBm"


class RunDetail(RunSummary):
    params: dict
    command: list[str]
    log: str
    report: dict | None


class PrefetchIn(BaseModel):
    west: float = Field(ge=-180, le=180)
    south: float = Field(ge=-90, le=90)
    east: float = Field(ge=-180, le=180)
    north: float = Field(ge=-90, le=90)

    @model_validator(mode="after")
    def _check(self):
        if self.north <= self.south:
            raise ValueError("north must be greater than south")
        return self


PlotOut.model_rebuild()
