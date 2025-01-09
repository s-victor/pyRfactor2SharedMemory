"""
rF2 Memory Map

Inherit Python mapping of The Iron Wolf's rF2 Shared Memory Tools,
with player-synchronized accessing (by S.Victor)
and cross-platform Linux support (by Bernat)
"""

from __future__ import annotations
import ctypes
import logging
import mmap
import platform
import threading
from copy import copy
from typing import Sequence
from time import monotonic, sleep

try:
    from . import rF2data
except ImportError:  # standalone, not package
    import rF2data

PLATFORM = platform.system()
MAX_VEHICLES = rF2data.rFactor2Constants.MAX_MAPPED_VEHICLES
INVALID_INDEX = -1

logger = logging.getLogger(__name__)


def platform_mmap(name: str, size: int, pid: str = "") -> mmap.mmap:
    """Platform memory mapping"""
    if PLATFORM == "Windows":
        return windows_mmap(name, size, pid)
    return linux_mmap(name, size)


def windows_mmap(name: str, size: int, pid: str) -> mmap.mmap:
    """Windows mmap"""
    return mmap.mmap(-1, size, f"{name}{pid}")


def linux_mmap(name: str, size: int) -> mmap.mmap:
    """Linux mmap"""
    file = open("/dev/shm/" + name, "a+b")
    if file.tell() == 0:
        file.write(b"\0" * size)
        file.flush()
    return mmap.mmap(file.fileno(), size)


def local_scoring_index(scor_veh: Sequence[rF2data.rF2VehicleScoring]) -> int:
    """Find local player scoring index

    Args:
        scor_veh: scoring mVehicles array.
    """
    for scor_idx, veh_info in enumerate(scor_veh):
        if veh_info.mIsPlayer:
            return scor_idx
    return INVALID_INDEX


class MMapControl:
    """Memory map control"""

    __slots__ = (
        "_mmap_name",
        "_buffer_data",
        "_mmap_instance",
        "_buffer_sharing",
        "update",
        "data",
    )

    def __init__(self, mmap_name: str, buffer_data: object) -> None:
        """Initialize memory map setting

        Args:
            mmap_name: mmap filename, ex. $rFactor2SMMP_Scoring$.
            buffer_data: buffer data class, ex. rF2data.rF2Scoring.
        """
        self._mmap_name = mmap_name
        self._buffer_data = buffer_data
        self._mmap_instance = None
        self._buffer_sharing = False
        self.update = None
        self.data = None

    def __del__(self):
        logger.info("sharedmemory: GC: MMap %s", self._mmap_name)

    def create(self, access_mode: int = 0, rf2_pid: str = "") -> None:
        """Create mmap instance & initial accessible copy

        Args:
            access_mode: 0 = copy access, 1 = direct access.
            rf2_pid: rF2 Process ID for accessing server data.
        """
        self._mmap_instance = platform_mmap(
            name=self._mmap_name,
            size=ctypes.sizeof(self._buffer_data),
            pid=rf2_pid
        )
        if access_mode:
            self.update = self.__buffer_share
        else:
            self.update = self.__buffer_copy
        self.update(True)
        mode = "Direct" if access_mode else "Copy"
        logger.info("sharedmemory: ACTIVE: %s (%s Access)", self._mmap_name, mode)

    def close(self) -> None:
        """Close memory mapping

        Create a final accessible mmap data copy before closing mmap instance.
        """
        self.data = copy(self.data)
        self._buffer_sharing = False
        try:
            self._mmap_instance.close()
            logger.info("sharedmemory: CLOSED: %s", self._mmap_name)
        except BufferError:
            logger.error("sharedmemory: buffer error while closing %s", self._mmap_name)
        self.update = None  # unassign update method (for proper garbage collection)

    def __buffer_share(self, _=None) -> None:
        """Share buffer direct access, may result desync"""
        if not self._buffer_sharing:
            self._buffer_sharing = True
            self.data = self._buffer_data.from_buffer(self._mmap_instance)

    def __buffer_copy(self, skip_check: bool = False) -> None:
        """Copy buffer access, check version before assign new data copy

        Args:
            skip_check: skip data version check.
        """
        temp = self._buffer_data.from_buffer_copy(self._mmap_instance)
        if temp.mVersionUpdateEnd == temp.mVersionUpdateBegin or skip_check:
            self.data = temp


class MMapDataSet:
    """Create mmap data set"""

    __slots__ = (
        "scor",
        "tele",
        "ext",
        "ffb",
    )

    def __init__(self) -> None:
        self.scor = MMapControl(rF2data.rFactor2Constants.MM_SCORING_FILE_NAME, rF2data.rF2Scoring)
        self.tele = MMapControl(rF2data.rFactor2Constants.MM_TELEMETRY_FILE_NAME, rF2data.rF2Telemetry)
        self.ext = MMapControl(rF2data.rFactor2Constants.MM_EXTENDED_FILE_NAME, rF2data.rF2Extended)
        self.ffb = MMapControl(rF2data.rFactor2Constants.MM_FORCE_FEEDBACK_FILE_NAME, rF2data.rF2ForceFeedback)

    def __del__(self):
        logger.info("sharedmemory: GC: MMapDataSet")

    def create_mmap(self, access_mode: int, rf2_pid: str) -> None:
        """Create mmap instance

        Args:
            access_mode: 0 = copy access, 1 = direct access.
            rf2_pid: rF2 Process ID for accessing server data.
        """
        self.scor.create(access_mode, rf2_pid)
        self.tele.create(access_mode, rf2_pid)
        self.ext.create(1, rf2_pid)
        self.ffb.create(1, rf2_pid)

    def close_mmap(self) -> None:
        """Close mmap instance"""
        self.scor.close()
        self.tele.close()
        self.ext.close()
        self.ffb.close()

    def update_mmap(self) -> None:
        """Update mmap data"""
        self.scor.update()
        self.tele.update()
        #self.ext.update()
        #self.ffb.update()


class SyncData:
    """Synchronize data with player ID

    Attributes:
        dataset: mmap data set.
        paused: Data update state (boolean).
        override_player_index: Player index override state (boolean).
        player_scor_index: Local player scoring index.
        player_scor: Local player scoring data.
        player_tele: Local player telemetry data.
    """

    __slots__ = (
        "_updating",
        "_update_thread",
        "_event",
        "_tele_indexes",
        "paused",
        "override_player_index",
        "player_scor_index",
        "player_scor",
        "player_tele",
        "dataset",
    )

    def __init__(self) -> None:
        self._updating = False
        self._update_thread = None
        self._event = threading.Event()
        self._tele_indexes = {_index: _index for _index in range(128)}

        self.paused = False
        self.override_player_index = False
        self.player_scor_index = INVALID_INDEX
        self.player_scor = None
        self.player_tele = None
        self.dataset = MMapDataSet()

    def __del__(self):
        logger.info("sharedmemory: GC: SyncData")

    def __sync_player_data(self) -> bool:
        """Sync local player data

        Returns:
            False, if no valid player scoring index found.
            True, set player data.
        """
        if not self.override_player_index:
            # Update scoring index
            scor_idx = local_scoring_index(self.dataset.scor.data.mVehicles)
            if scor_idx == INVALID_INDEX:
                return False  # index not found, not synced
            self.player_scor_index = scor_idx
        # Set player data
        self.player_scor = self.dataset.scor.data.mVehicles[self.player_scor_index]
        self.player_tele = self.dataset.tele.data.mVehicles[self.sync_tele_index(self.player_scor_index)]
        return True  # found index, synced

    @staticmethod
    def __update_tele_indexes(tele_data: rF2data.rF2Telemetry, tele_indexes: dict) -> None:
        """Update telemetry player index dictionary for quick reference

        Telemetry index can be different from scoring index.
        Use mID matching to match telemetry index.

        Args:
            tele_data: Telemetry data.
            tele_indexes: Telemetry mID:index reference dictionary.
        """
        for tele_idx, veh_info in zip(range(tele_data.mNumVehicles), tele_data.mVehicles):
            tele_indexes[veh_info.mID] = tele_idx

    def sync_tele_index(self, scor_idx: int) -> int:
        """Sync telemetry index

        Use scoring index to find scoring mID,
        then match with telemetry mID in reference dictionary
        to find telemetry index.

        Args:
            scor_idx: Player scoring index.

        Returns:
            Player telemetry index.
        """
        return self._tele_indexes.get(
            self.dataset.scor.data.mVehicles[scor_idx].mID, INVALID_INDEX)

    def start(self, access_mode: int, rf2_pid: str) -> None:
        """Update & sync mmap data copy in separate thread

        Args:
            access_mode: 0 = copy access, 1 = direct access.
            rf2_pid: rF2 Process ID for accessing server data.
        """
        if self._updating:
            logger.warning("sharedmemory: UPDATING: already started")
        else:
            self._updating = True
            # Initialize mmap data
            self.dataset.create_mmap(access_mode, rf2_pid)
            self.__update_tele_indexes(self.dataset.tele.data, self._tele_indexes)
            if not self.__sync_player_data():
                self.player_scor = self.dataset.scor.data.mVehicles[INVALID_INDEX]
                self.player_tele = self.dataset.tele.data.mVehicles[INVALID_INDEX]
            # Setup updating thread
            self._event.clear()
            self._update_thread = threading.Thread(target=self.__update, daemon=True)
            self._update_thread.start()
            logger.info("sharedmemory: UPDATING: thread started")
            logger.info("sharedmemory: player index override: %s", self.override_player_index)
            logger.info("sharedmemory: server process ID: %s", rf2_pid if rf2_pid else "DISABLED")

    def stop(self) -> None:
        """Join and stop updating thread, close mmap"""
        if self._updating:
            self._event.set()
            self._updating = False
            self._update_thread.join()
            # Make final copy before close, otherwise mmap won't close if using direct access
            self.player_scor = copy(self.player_scor)
            self.player_tele = copy(self.player_tele)
            self.dataset.close_mmap()
        else:
            logger.warning("sharedmemory: UPDATING: already stopped")

    def __update(self) -> None:
        """Update synced player data"""
        self.paused = False  # make sure initial pause state is false
        freezed_version = 0  # store freezed update version number
        last_version_update = 0  # store last update version number
        last_update_time = 0.0
        data_freezed = True  # whether data is freezed
        reset_counter = 0
        update_delay = 0.5  # longer delay while inactive

        while not self._event.wait(update_delay):
            self.dataset.update_mmap()
            self.__update_tele_indexes(self.dataset.tele.data, self._tele_indexes)
            # Update player data & index
            if not data_freezed:
                # Get player data
                data_synced = self.__sync_player_data()
                # Pause if local player index no longer exists, 5 tries
                if data_synced:
                    reset_counter = 0
                    self.paused = False
                elif reset_counter < 6:
                    reset_counter += 1
                    if reset_counter == 5:
                        self.paused = True
                        logger.info("sharedmemory: UPDATING: player data paused")

            version_update = self.dataset.scor.data.mVersionUpdateEnd
            if last_version_update != version_update:
                last_version_update = version_update
                last_update_time = monotonic()

            if data_freezed:
                # Check while IN freeze state
                if freezed_version != last_version_update:
                    update_delay = 0.01
                    self.paused = data_freezed = False
                    logger.info(
                        "sharedmemory: UPDATING: resumed, data version %s",
                        last_version_update,
                    )
            # Check while NOT IN freeze state
            # Set freeze state if data stopped updating after 2s
            elif monotonic() - last_update_time > 2:
                update_delay = 0.5
                self.paused = data_freezed = True
                freezed_version = last_version_update
                logger.info(
                    "sharedmemory: UPDATING: paused, data version %s",
                    freezed_version,
                )

        logger.info("sharedmemory: UPDATING: thread stopped")


class RF2SM:
    """RF2 shared memory data output"""

    __slots__ = (
        "_sync",
        "_access_mode",
        "_rf2_pid",
        "_scor",
        "_tele",
        "_ext",
        "_ffb",
    )

    def __init__(self) -> None:
        self._sync = SyncData()
        self._access_mode = 0
        self._rf2_pid = ""
        # Assign mmap instance
        self._scor = self._sync.dataset.scor
        self._tele = self._sync.dataset.tele
        self._ext = self._sync.dataset.ext
        self._ffb = self._sync.dataset.ffb

    def __del__(self):
        logger.info("sharedmemory: GC: RF2SM")

    def start(self) -> None:
        """Start data updating thread"""
        self._sync.start(self._access_mode, self._rf2_pid)

    def stop(self) -> None:
        """Stop data updating thread"""
        self._sync.stop()

    def setPID(self, pid: str = "") -> None:
        """Set rF2 process ID for connecting to server data"""
        self._rf2_pid = str(pid)

    def setMode(self, mode: int = 0) -> None:
        """Set rF2 mmap access mode

        Args:
            mode: 0 = copy access, 1 = direct access
        """
        self._access_mode = mode

    def setPlayerOverride(self, state: bool = False) -> None:
        """Enable player index override state"""
        self._sync.override_player_index = state

    def setPlayerIndex(self, index: int = INVALID_INDEX) -> None:
        """Manual override player index"""
        self._sync.player_scor_index = min(max(index, INVALID_INDEX), MAX_VEHICLES - 1)

    @property
    def rf2ScorInfo(self) -> rF2data.rF2ScoringInfo:
        """rF2 scoring info data"""
        return self._scor.data.mScoringInfo

    def rf2ScorVeh(self, index: int | None = None) -> rF2data.rF2VehicleScoring:
        """rF2 scoring vehicle data

        Specify index for specific player.

        Args:
            index: None for local player.
        """
        if index is None:
            return self._sync.player_scor
        return self._scor.data.mVehicles[index]

    def rf2TeleVeh(self, index: int | None = None) -> rF2data.rF2VehicleTelemetry:
        """rF2 telemetry vehicle data

        Specify index for specific player.

        Args:
            index: None for local player.
        """
        if index is None:
            return self._sync.player_tele
        return self._tele.data.mVehicles[self._sync.sync_tele_index(index)]

    @property
    def rf2Ext(self) -> rF2data.rF2Extended:
        """rF2 extended data"""
        return self._ext.data

    @property
    def rf2Ffb(self) -> rF2data.rF2ForceFeedback:
        """rF2 force feedback data"""
        return self._ffb.data

    @property
    def playerIndex(self) -> int:
        """rF2 local player's scoring index"""
        return self._sync.player_scor_index

    def isPlayer(self, index: int) -> bool:
        """Check whether index is player"""
        if self._sync.override_player_index:
            return self._sync.player_scor_index == index
        return self._scor.data.mVehicles[index].mIsPlayer

    @property
    def isPaused(self) -> bool:
        """Check whether data stopped updating"""
        return self._sync.paused


def test_api():
    """API test run"""
    # Add logger
    test_handler = logging.StreamHandler()
    logger.setLevel(logging.INFO)
    logger.addHandler(test_handler)

    # Test run
    SEPARATOR = "=" * 50
    print("Test API - Start")
    info = RF2SM()
    info.setMode(1)  # set direct access
    info.setPID("")
    info.setPlayerOverride(True)  # enable player override
    info.setPlayerIndex(0)  # set player index to 0
    info.start()
    sleep(0.2)

    print(SEPARATOR)
    print("Test API - Restart")
    info.stop()
    info.setMode()  # set copy access
    info.setPlayerOverride()  # disable player override
    info.start()

    print(SEPARATOR)
    print("Test API - Read")
    version = info.rf2Ext.mVersion.decode()
    driver = info.rf2ScorVeh(0).mDriverName.decode(encoding="iso-8859-1")
    track = info.rf2ScorInfo.mTrackName.decode(encoding="iso-8859-1")
    print(f"plugin version: {version if version else 'not running'}")
    print(f"driver name   : {driver if version else 'not running'}")
    print(f"track name    : {track if version else 'not running'}")

    print(SEPARATOR)
    print("Test API - Close")
    info.stop()


if __name__ == "__main__":
    test_api()
