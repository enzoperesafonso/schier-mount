import logging
from comm import MountSafetyError


class SafetyMonitor:
    def __init__(self, config):
        self.cfg = config
        self.logger = logging.getLogger("SafetyMonitor")

        # Track the last known status to detect changes
        self.last_status = {'ra': {}, 'dec': {}}

    def check_status(self, ra_status, dec_status):
        """
        Analyzes the status bits returned by the mount.
        Returns a list of active faults, or empty list if safe.
        """
        faults = []

        # 1. Check Hardware Flags (E-Stop, Amp Disable)
        # Derived from mountd_main.c error checks
        if ra_status.get('estop') or dec_status.get('estop'):
            faults.append("E-STOP ACTIVE")

        if ra_status.get('amp_disabled') or dec_status.get('amp_disabled'):
            faults.append("AMPLIFIER DISABLED")

        # 2. Check Hardware Limits
        # Derived from mountd_main.c limit checks
        if ra_status.get('pos_limit'): faults.append("RA_POS_LIMIT")
        if ra_status.get('neg_limit'): faults.append("RA_NEG_LIMIT")
        if dec_status.get('pos_limit'): faults.append("DEC_POS_LIMIT")
        if dec_status.get('neg_limit'): faults.append("DEC_NEG_LIMIT")

        return faults

    def validate_target(self, enc_ra, enc_dec):
        """
        Soft Limit Check: Prevent sending the mount to a dangerous place.
        """
        # RA Ranges from configuration
        if not (self.cfg.ra_soft_min <= enc_ra <= self.cfg.ra_soft_max):
            raise MountSafetyError(f"Target RA {enc_ra} out of bounds")

        # Dec Ranges
        if not (self.cfg.dec_soft_min <= enc_dec <= self.cfg.dec_soft_max):
            raise MountSafetyError(f"Target Dec {enc_dec} out of bounds")