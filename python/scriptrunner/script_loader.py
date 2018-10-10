# This file is part of scriptrunner.
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

__all__ = ["ScriptLoader"]

import asyncio
import os.path

import SALPY_ScriptLoader
import salobj
from .loader_model import LoaderModel

_LOAD_TIMEOUT = 5  # seconds


class ScriptLoader(salobj.BaseCsc):
    """CSC to load and configure scripts, so they can be run.

    Parameters
    ----------
    standardpath : `str`, `bytes` or `os.PathLike`
        Path to standard modules.
    externalpath : `str`, `bytes` or `os.PathLike`
        Path to external modules.

    Notes
    -----

    Use the `ScriptLoader` as follows:

    * Send the ``load`` command to the ``ScriptLoader`` to load
      and configure a script.
    * The loaded script will come up as a ``Script`` SAL component
      with a unique index.
    * `ScriptLoader` will output a ``script_info`` message which includes
      the ID of the ``load`` command and the index of the ``Script``
      SAL component.
    * It is crucial to pay attention to the command ID in the
      ``script_info`` block, in order to reliably determine the index
      of the newly loaded script.
    * TO DO (this will be implemented as part of TSS-3148):
      Once the script is fully loaded, the loader will send the
      configuration specified in the ``load`` command.
    * Configuring the script causes it to output an estimate of its
      duration and puts the script into the CONFIGURED state,
      which enables it to be run.
    * To run the script issue the ``run`` command to the ``Script`` SAL
      component (not the `ScriptLoader`).
    """
    def __init__(self, standardpath, externalpath):
        if not os.path.isdir(standardpath):
            raise ValueError(f"No such dir standardpath={standardpath}")
        if not os.path.isdir(externalpath):
            raise ValueError(f"No such dir externalpath={externalpath}")

        super().__init__(SALPY_ScriptLoader, "ScriptLoader:0")
        self.model = LoaderModel(standardpath=standardpath, externalpath=externalpath,
                                 timefunc=self.controller.salinfo.manager.getCurrentTime)
        self.do_list_available()

    def do_load(self, id_data):
        """Load a script.

        Start a script SAL component, but don't run the script.
        """
        AckType = self.controller.cmd_load.AckType
        data = id_data.data

        async def load(id_data):
            """Load a script and report command completion."""
            ack = AckType()
            load_coro = self.model.load(id_data=id_data,
                                        path=data.path,
                                        is_standard=data.is_standard,
                                        callback=self.put_script_info)
            try:
                await asyncio.wait_for(load_coro, timeout=_LOAD_TIMEOUT)
                ack.ack = self.controller.salinfo.lib.SAL__CMD_COMPLETE
            except TimeoutError:
                ack.ack = self.controller.salinfo.lib.SAL__CMD_TIMEOUT
            except Exception as e:
                ack.ack = self.controller.salinfo.lib.SAL__CMD_FAILED
                ack.result = str(e)
            self.controller.cmd_load.ack(id_data, ack.ack, ack.error, ack.result)

        asyncio.ensure_future(load(id_data))

        ack = AckType()
        ack.ack = self.controller.salinfo.lib.SAL__CMD_INPROGRESS
        return ack

    def do_terminate(self, id_data):
        """Terminate the specified script by sending SIGTERM.
        """
        self.model.terminate(id_data.data.index)

    def do_list_available(self, id_data=None):
        """List available scripts.

        Parameters
        ----------
        id_data : `salobj.topics.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        scripts = self.model.findscripts()
        evtdata = self.controller.evt_available_scripts.DataType()
        evtdata.standard = ":".join(scripts.standard)
        evtdata.external = ":".join(scripts.external)
        self.controller.evt_available_scripts.put(evtdata, 1)

    def do_list_loaded(self, id_data):
        """List loaded scripts.

        Parameters
        ----------
        id_data : `salobj.topics.CommandIdData` (optional)
            Command ID and data. Ignored.
        """
        asyncio.ensure_future(self.list_loaded(id_data))
        ack = self.controller.cmd_list_loaded.AckType()
        ack.ack = self.controller.salinfo.lib.SAL__CMD_INPROGRESS
        return ack

    async def list_loaded(self, id_data):
        """Asynchronous implementation of list_loaded command.

        Release the event loop after putting information for each script,
        in case there are a lot of scripts.
        """
        ack = self.controller.cmd_list_loaded.AckType()
        try:
            for script_info in self.model.info.values():
                self.put_script_info(script_info)
                await asyncio.sleep(0)
            ack.ack = self.controller.salinfo.lib.SAL__CMD_COMPLETE
        except Exception as e:
            ack.ack = self.controller.salinfo.lib.SAL__CMD_FAILED
            ack.result = str(e)
        finally:
            self.controller.cmd_list_loaded.ack(id_data, ack=ack.ack, error=ack.error, result=ack.result)

    def put_script_info(self, script_info, returncode=None):
        """Output information about a script using the script_info event.

        Intended as a callback for self.model.task,
        and only if the script is successfully loaded.

        Parameters
        ----------
        script_info : `scriptrunner.ScriptInfo`
            Information about the script
        returncode : `int` (optional)
            Ignored, but needed for use as a callback.
        """
        if script_info is None:
            return

        sallib = self.controller.salinfo.lib
        evtdata = self.controller.evt_script_info.DataType()
        evtdata.cmd_id = script_info.cmd_id
        evtdata.index = script_info.index
        evtdata.path = script_info.path
        evtdata.is_standard = script_info.is_standard
        evtdata.timestamp_start = script_info.timestamp_start
        evtdata.timestamp_end = script_info.timestamp_end
        returncode = script_info.process.returncode
        if returncode is None:
            evtdata.process_state = sallib.script_info_LOADED
        else:
            # the process is finished; delete the information
            del self.model.info[script_info.index]
            if returncode == 0:
                evtdata.process_state = sallib.script_info_COMPLETE
            elif returncode > 0:
                evtdata.process_state = sallib.script_info_FAILED
            else:
                evtdata.process_state = sallib.script_info_TERMINATED
        self.controller.evt_script_info.put(evtdata, 1)
