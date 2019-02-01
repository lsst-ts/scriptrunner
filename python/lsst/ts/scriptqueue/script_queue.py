# This file is part of ts_scriptqueue.
#
# Developed for the LSST Telescope and Site Systems.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

__all__ = ["ScriptQueue"]

import asyncio
import os.path

import numpy as np

import SALPY_ScriptQueue
from lsst.ts import salobj
from .queue_model import QueueModel, ScriptInfo

SCRIPT_INDEX_MULT = 100000
"""Minimum Script SAL index is ScriptQueue SAL index * SCRIPT_INDEX_MULT
and the maximum is SCRIPT_INDEX_MULT-1 more.
"""
_MAX_SCRIPTQUEUE_INDEX = salobj.MAX_SAL_INDEX//SCRIPT_INDEX_MULT - 1
_LOAD_TIMEOUT = 20  # seconds


class ScriptQueue(salobj.BaseCsc):
    """CSC to load and configure scripts, so they can be run.

    Parameters
    ----------
    index : `int`
        ScriptQueue SAL component index:

        * 1 for the Main telescope.
        * 2 for AuxTel.
        * Any allowed value (see ``Raises``) for unit tests.
    standardpath : `str`, `bytes` or `os.PathLike`
        Path to standard SAL scripts.
    externalpath : `str`, `bytes` or `os.PathLike`
        Path to external SAL scripts.
    verbose : `bool`
        If True then print diagnostic messages to stdout.

    Raises
    ------
    ValueError
        If ``index`` < 0 or > MAX_SAL_INDEX//100,000 - 1.
        If ``standardpath`` or ``externalpath`` is not an existing dir.

    Notes
    -----
    .. _script_queue_basic_usage:

    Basic usage:

    * Send the ``add`` command to the ``ScriptQueue`` to add a script
      to the queue. The added script is loaded in a subprocess
      as a new ``Script`` SAL component with a unique SAL index:

      * The script's SAL index is used to uniquely identify the script
        for commands such as ``move`` and ``stopScript``.
      * The index is returned as the ``result`` field of
        the final acknowledgement of the ``add`` command.
      * The first script loaded has index ``min_sal_index``,
        the next has index ``min_sal_index+1``,
        then  ``min_sal_index+2``, ... ``max_sal_index``,
        then wrap around to start over at ``min_sal_index``.
      * The minimum SAL script index is 100,000 * the SAL index
        of `ScriptQueue`: 100,000 for the main telescope
        and 200,000 for the auxiliary telescope.
        The maximum is, naturally, 99,999 more than that.

    * Once a script is added, it reports its state as
      `ScriptState.UNCONFIGURED`. At that point the `ScriptQueue`
      configures it, using the configuration specified in the
      ``add`` command.
    * Configuring a script causes it to output the ``metadata`` event,
      which includes an estimated duration, and changes the script's
      state to `ScriptState.CONFIGURED`. This means it can now be run.
    * When the current script is finished, its information is moved
      to a history list, in order to support requeueing old scripts. Then:

      * If the queue is running, then when the first script in the queue
        has been configured, it is moved to the ``current`` slot and run.
      * If the queue is paused, then the current slot is left empty
        and no new script is run.
    * Once a script has finished running, its information is moved to
      a history list, which is output as part of the ``queue`` event.
      The history list allows ``requeue`` to work with scripts that
      have already run.

    Events:

    * As each script is added or changes state `ScriptQueue` outputs
      a ``script_info`` event which includes the script's SAL index,
      path and state.
    * As the script queue changes state `ScriptQueue` outputs the
      ``queue`` event listing the SAL indices of scripts on the queue,
      the currently running script, and scripts that have been run
      (the history list).
    * When each script is configured, the script (not `ScriptQueue`)
      outputs a ``metadata`` event that includes estimated duration.
    """
    def __init__(self, index, standardpath, externalpath, verbose=False):
        if index < 0 or index > _MAX_SCRIPTQUEUE_INDEX:
            raise ValueError(f"index {index} must be >= 0 and <= {_MAX_SCRIPTQUEUE_INDEX}")
        if not os.path.isdir(standardpath):
            raise ValueError(f"No such dir standardpath={standardpath}")
        if not os.path.isdir(externalpath):
            raise ValueError(f"No such dir externalpath={externalpath}")
        self.verbose = verbose

        min_sal_index = index * SCRIPT_INDEX_MULT
        max_sal_index = min_sal_index + SCRIPT_INDEX_MULT - 1
        if max_sal_index > salobj.MAX_SAL_INDEX:
            raise ValueError(f"index {index} too large and a bug let this slip through")
        self.model = QueueModel(standardpath=standardpath,
                                externalpath=externalpath,
                                queue_callback=self.put_queue,
                                script_callback=self.put_script,
                                min_sal_index=min_sal_index,
                                max_sal_index=max_sal_index,
                                verbose=verbose)

        super().__init__(SALPY_ScriptQueue, index)
        self.put_queue()

    def do_showAvailableScripts(self, id_data=None):
        """Output a list of available scripts.

        Parameters
        ----------
        id_data : `salobj.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        self.assert_enabled("showAvailableScripts")
        scripts = self.model.find_available_scripts()
        self.evt_availableScripts.set_put(
            standard=":".join(scripts.standard),
            external=":".join(scripts.external),
            force_output=True,
        )

    def do_showQueue(self, id_data):
        """Output the queue event.

        Parameters
        ----------
        id_data : `salobj.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        self.assert_enabled("showQueue")
        self.put_queue()

    def do_showScript(self, id_data):
        """Output the script event for one script.

        Parameters
        ----------
        id_data : `salobj.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        self.assert_enabled("showScript")
        script_info = self.model.get_script_info(id_data.data.salIndex,
                                                 search_history=True)
        self.put_script(script_info, force_output=True)

    def do_pause(self, id_data):
        """Pause the queue. A no-op if already paused.

        Unlike most commands, this can be issued in any state.

        Parameters
        ----------
        id_data : `salobj.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        self.model.running = False

    def do_resume(self, id_data):
        """Run the queue. A no-op if already running.

        Parameters
        ----------
        id_data : `salobj.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        self.assert_enabled("resume")
        self.model.running = True

    async def do_add(self, id_data):
        """Add a script to the queue.

        Start and configure a script SAL component, but don't run it.

        On success the ``result`` field of the final acknowledgement
        contains ``str(index)`` where ``index`` is the SAL index
        of the added Script.
        """
        self.assert_enabled("add")
        script_info = ScriptInfo(
            index=self.model.next_sal_index,
            cmd_id=id_data.cmd_id,
            is_standard=id_data.data.isStandard,
            path=id_data.data.path,
            config=id_data.data.config,
            descr=id_data.data.descr,
            verbose=self.verbose,
        )
        await self.model.add(
            script_info=script_info,
            location=id_data.data.location,
            location_sal_index=id_data.data.locationSalIndex,
        )
        return self.salinfo.makeAck(ack=SALPY_ScriptQueue.SAL__CMD_COMPLETE, result=str(script_info.index))

    def do_move(self, id_data):
        """Move a script within the queue.
        """
        self.assert_enabled("move")
        self.model.move(sal_index=id_data.data.salIndex,
                        location=id_data.data.location,
                        location_sal_index=id_data.data.locationSalIndex)

    async def do_requeue(self, id_data):
        """Put a script back on the queue with the same configuration.
        """
        self.assert_enabled("requeue")
        await self.model.requeue(
            sal_index=id_data.data.salIndex,
            cmd_id=id_data.cmd_id,
            location=id_data.data.location,
            location_sal_index=id_data.data.locationSalIndex,
        )

    async def do_stopScripts(self, id_data):
        """Stop one or more queued scripts and/or the current script.

        If you stop the current script, it is moved to the history.
        If you stop queued scripts they are not not moved to the history.
        """
        self.assert_enabled("stopScripts")
        data = id_data.data
        if data.length <= 0:
            raise salobj.ExpectedError(f"length={data.length} must be positive")
        timeout = 5 + 0.2*data.length
        await asyncio.wait_for(self.model.stop_scripts(sal_indices=data.salIndices[0:data.length],
                                                       terminate=data.terminate), timeout)

    def report_summary_state(self):
        super().report_summary_state()
        enabled = self.summary_state == salobj.State.ENABLED
        self.model.enabled = enabled
        if enabled:
            self.do_showAvailableScripts()

    def put_queue(self):
        """Output the queued scripts as a ``queue`` event.

        The data is put even if the queue has not changed. That way commands
        which alter the queue can rely on the event being published,
        even if the command has no effect (e.g. moving a script before itself).
        """
        sal_indices = np.zeros_like(self.evt_queue.data.salIndices)
        indlen = min(len(self.model.queue), len(sal_indices))
        sal_indices[0:indlen] = [info.index for info in self.model.queue][0:indlen]

        past_sal_indices = np.zeros_like(self.evt_queue.data.pastSalIndices)
        pastlen = min(len(self.model.history), len(past_sal_indices))
        past_sal_indices[0:pastlen] = [info.index for info in self.model.history][0:pastlen]

        if self.verbose:
            print(f"put_queue: enabled={self.model.enabled}, running={self.model.running}, "
                  f"currentSalIndex={self.model.current_index}, "
                  f"salIndices={sal_indices[0:indlen]}, "
                  f"pastSalIndices={past_sal_indices[0:pastlen]}")
        self.evt_queue.set_put(
            enabled=self.model.enabled,
            running=self.model.running,
            currentSalIndex=self.model.current_index,
            length=indlen,
            salIndices=sal_indices,
            pastLength=pastlen,
            pastSalIndices=past_sal_indices,
            force_output=True)

    def put_script(self, script_info, force_output=False):
        """Output information about a script as a ``script`` event.

        Designed to be used as a QueueModel script_callback.

        Parameters
        ----------
        script_info : `ScriptInfo`
            Information about the script.
        force_output : `bool` (optional)
            If True the output even if not changed.
        """
        if script_info is None:
            return

        if self.verbose:
            print(f"put_script: index={script_info.index}, "
                  f"process_state={script_info.process_state}, "
                  f"script_state={script_info.script_state}")
        self.evt_script.set_put(
            cmdId=script_info.cmd_id,
            salIndex=script_info.index,
            path=script_info.path,
            isStandard=script_info.is_standard,
            timestamp=script_info.timestamp,
            duration=script_info.duration,
            processState=script_info.process_state,
            scriptState=script_info.script_state,
            force_output=force_output,
        )
