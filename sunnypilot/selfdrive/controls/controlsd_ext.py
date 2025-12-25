"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import time

import cereal.messaging as messaging
from cereal import log, custom

from opendbc.car import CanBus, structs
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.sunnypilot import PARAMS_UPDATE_PERIOD
from openpilot.sunnypilot.livedelay.helpers import get_lat_delay
from openpilot.sunnypilot.modeld.modeld_base import ModelStateBase
from openpilot.sunnypilot.selfdrive.controls.lib.blinker_pause_lateral import BlinkerPauseLateral
from opendbc.car.chrysler.chryslercan import create_cruise_buttons
from opendbc.car.chrysler.values import CAR

ACC_BUS = CanBus.ADAS_CAN

CHRYSLER_PACIFICA_PLATFORMS = (
  CAR.CHRYSLER_PACIFICA_2018_HYBRID,
  CAR.CHRYSLER_PACIFICA_2019_HYBRID,
  CAR.CHRYSLER_PACIFICA_2018,
  CAR.CHRYSLER_PACIFICA_2020,
  CAR.DODGE_DURANGO
)


class ControlsExt(ModelStateBase):
  def __init__(self, CP: structs.CarParams, CP_SP: custom.CarParamsSP, params: Params, CI):
    ModelStateBase.__init__(self)
    self.CP = CP
    self.params = params
    self._param_update_time: float = 0.0
    self.blinker_pause_lateral = BlinkerPauseLateral()

    self.CP_SP = CP_SP
    self.CI = CI
    self.cruise_auto_on_done = False

    self.sm_services_ext = ["radarState", "selfdriveStateSP"]
    self.pm_services_ext = ["carControlSP"]

  def get_params_sp(self, sm: messaging.SubMaster) -> None:
    if time.monotonic() - self._param_update_time > PARAMS_UPDATE_PERIOD:
      self.blinker_pause_lateral.get_params()

      if self.CP.lateralTuning.which() == "torque":
        self.lat_delay = get_lat_delay(self.params, sm["liveDelay"].lateralDelay)

      self._param_update_time = time.monotonic()

  def get_lat_active(self, sm: messaging.SubMaster) -> bool:
    if self.blinker_pause_lateral.update(sm["carState"]):
      return False

    ss_sp = sm["selfdriveStateSP"]
    if ss_sp.mads.available:
      return bool(ss_sp.mads.active)

    # MADS not available, use stock state to engage
    return bool(sm["selfdriveState"].active)

  def get_lead_data(self, ld: log.RadarState.LeadData) -> dict:
    return {
      "dRel": ld.dRel,
      "yRel": ld.yRel,
      "vRel": ld.vRel,
      "aRel": ld.aRel,
      "vLead": ld.vLead,
      "dPath": ld.dPath,
      "vLat": ld.vLat,
      "vLeadK": ld.vLeadK,
      "aLeadK": ld.aLeadK,
      "fcw": ld.fcw,
      "status": ld.status,
      "aLeadTau": ld.aLeadTau,
      "modelProb": ld.modelProb,
      "radar": ld.radar,
      "radarTrackId": ld.radarTrackId,
    }

  def state_control_ext(self, sm: messaging.SubMaster) -> custom.CarControlSP:
    CC_SP = custom.CarControlSP.new_message()

    CC_SP.leadOne = self.get_lead_data(sm["radarState"].leadOne)
    CC_SP.leadTwo = self.get_lead_data(sm["radarState"].leadTwo)

    # MADS state
    CC_SP.mads = sm["selfdriveStateSP"].mads

    CC_SP.intelligentCruiseButtonManagement = sm["selfdriveStateSP"].intelligentCruiseButtonManagement

    return CC_SP

  def auto_cruise_on_at_start(self, sm: messaging.SubMaster):
    can_sends = []
    CS = sm["carState"]

    auto_on_enabled = self.params.get_bool("AutoCruiseOnWithResume")
    # Only attempt to auto-on if sunnypilot is enabled and cruise is available but not engaged
    # We trigger specifically when cruise becomes available for the first time
    if not self.cruise_auto_on_done and auto_on_enabled and sm["selfdriveState"].enabled and \
       CS.cruiseState.available and not CS.cruiseState.enabled:
      # Check if compatible Chrysler platform and send a resume button press
      if self.CP.carFingerprint in CHRYSLER_PACIFICA_PLATFORMS:
        # Increment CI.frame to avoid sending the same frame multiple times if called rapidly
        self.CI.frame += 1
        can_sends.append(create_cruise_buttons(self.CI.packer, self.CI.frame, ACC_BUS, resume=True))
        self.cruise_auto_on_done = True
        cloudlog.info("sunnypilot: Auto-enabled adaptive cruise control.")

    return can_sends

  def publish_ext(self, CC_SP: custom.CarControlSP, sm: messaging.SubMaster, pm: messaging.PubMaster, can_sends: list) -> None:
    # Add any CAN sends from auto_cruise_on_at_start
    for can_send in can_sends:
      pm.send("sendcan", can_send)

    cc_sp_send = messaging.new_message("carControlSP")
    cc_sp_send.valid = sm["carState"].canValid
    cc_sp_send.carControlSP = CC_SP

    pm.send("carControlSP", cc_sp_send)

  def run_ext(self, sm: messaging.SubMaster, pm: messaging.PubMaster) -> None:
    CC_SP = self.state_control_ext(sm)
    can_sends = self.auto_cruise_on_at_start(sm)
    self.publish_ext(CC_SP, sm, pm, can_sends)
