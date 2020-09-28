# Copyright 2013 Rackspace, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import binascii
import multiprocessing
import os
import time

from ironic_lib import disk_utils
import mock
import netifaces
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_utils import units
import pyudev
import six
from stevedore import extension

from ironic_python_agent import errors
from ironic_python_agent import hardware
from ironic_python_agent import netutils
from ironic_python_agent.tests.unit import base
from ironic_python_agent import utils

CONF = cfg.CONF

CONF.import_opt('disk_wait_attempts', 'ironic_python_agent.config')
CONF.import_opt('disk_wait_delay', 'ironic_python_agent.config')

HDPARM_INFO_TEMPLATE = (
    '/dev/sda:\n'
    '\n'
    'ATA device, with non-removable media\n'
    '\tModel Number:       7 PIN  SATA FDM\n'
    '\tSerial Number:      20131210000000000023\n'
    '\tFirmware Revision:  SVN406\n'
    '\tTransport:          Serial, ATA8-AST, SATA 1.0a, SATA II Extensions, '
        'SATA Rev 2.5, SATA Rev 2.6, SATA Rev 3.0\n'
    'Standards: \n'
    '\tSupported: 9 8 7 6 5\n'
    '\tLikely used: 9\n'
    'Configuration: \n'
    '\tLogical\t\tmax\tcurrent\n'
    '\tcylinders\t16383\t16383\n'
    '\theads\t\t16\t16\n'
    '\tsectors/track\t63\t63\n'
    '\t--\n'
    '\tCHS current addressable sectors:   16514064\n'
    '\tLBA    user addressable sectors:   60579792\n'
    '\tLBA48  user addressable sectors:   60579792\n'
    '\tLogical  Sector size:                   512 bytes\n'
    '\tPhysical Sector size:                   512 bytes\n'
    '\tLogical Sector-0 offset:                  0 bytes\n'
    '\tdevice size with M = 1024*1024:       29579 MBytes\n'
    '\tdevice size with M = 1000*1000:       31016 MBytes (31 GB)\n'
    '\tcache/buffer size  = unknown\n'
    '\tForm Factor: 2.5 inch\n'
    '\tNominal Media Rotation Rate: Solid State Device\n'
    'Capabilities: \n'
    '\tLBA, IORDY(can be disabled)\n'
    '\tQueue depth: 32\n'
    '\tStandby timer values: spec\'d by Standard, no device specific '
        'minimum\n'
    '\tR/W multiple sector transfer: Max = 1\tCurrent = 1\n'
    '\tDMA: mdma0 mdma1 mdma2 udma0 udma1 udma2 udma3 udma4 *udma5\n'
    '\t     Cycle time: min=120ns recommended=120ns\n'
    '\tPIO: pio0 pio1 pio2 pio3 pio4\n'
    '\t     Cycle time: no flow control=120ns  IORDY flow '
        'control=120ns\n'
    'Commands/features: \n'
    '\tEnabled\tSupported:\n'
    '\t   *\tSMART feature set\n'
    '\t    \tSecurity Mode feature set\n'
    '\t   *\tPower Management feature set\n'
    '\t   *\tWrite cache\n'
    '\t   *\tLook-ahead\n'
    '\t   *\tHost Protected Area feature set\n'
    '\t   *\tWRITE_BUFFER command\n'
    '\t   *\tREAD_BUFFER command\n'
    '\t   *\tNOP cmd\n'
    '\t    \tSET_MAX security extension\n'
    '\t   *\t48-bit Address feature set\n'
    '\t   *\tDevice Configuration Overlay feature set\n'
    '\t   *\tMandatory FLUSH_CACHE\n'
    '\t   *\tFLUSH_CACHE_EXT\n'
    '\t   *\tWRITE_{DMA|MULTIPLE}_FUA_EXT\n'
    '\t   *\tWRITE_UNCORRECTABLE_EXT command\n'
    '\t   *\tGen1 signaling speed (1.5Gb/s)\n'
    '\t   *\tGen2 signaling speed (3.0Gb/s)\n'
    '\t   *\tGen3 signaling speed (6.0Gb/s)\n'
    '\t   *\tNative Command Queueing (NCQ)\n'
    '\t   *\tHost-initiated interface power management\n'
    '\t   *\tPhy event counters\n'
    '\t   *\tDMA Setup Auto-Activate optimization\n'
    '\t    \tDevice-initiated interface power management\n'
    '\t   *\tSoftware settings preservation\n'
    '\t    \tunknown 78[8]\n'
    '\t   *\tSMART Command Transport (SCT) feature set\n'
    '\t   *\tSCT Error Recovery Control (AC3)\n'
    '\t   *\tSCT Features Control (AC4)\n'
    '\t   *\tSCT Data Tables (AC5)\n'
    '\t   *\tData Set Management TRIM supported (limit 2 blocks)\n'
    'Security: \n'
    '\tMaster password revision code = 65534\n'
    '\t%(supported)s\n'
    '\t%(enabled)s\n'
    '\t%(locked)s\n'
    '\t%(frozen)s\n'
    '\tnot\texpired: security count\n'
    '\t%(enhanced_erase)s\n'
    '\t24min for SECURITY ERASE UNIT. 24min for ENHANCED SECURITY '
        'ERASE UNIT.\n'
    'Checksum: correct\n'
)  # noqa
# NOTE(jroll) noqa here is to dodge E131 (indent rules). Since this is a
# massive multi-line string (with specific whitespace formatting), it's easier
# for a human to parse it with indentations on line continuations. The other
# option would be to ignore the 79-character limit here. Ew.

BLK_DEVICE_TEMPLATE = (
    'KNAME="sda" MODEL="TinyUSB Drive" SIZE="3116853504" '
    'ROTA="0" TYPE="disk" SERIAL="123"\n'
    'KNAME="sdb" MODEL="Fastable SD131 7" SIZE="10737418240" '
    'ROTA="0" TYPE="disk"\n'
    'KNAME="sdc" MODEL="NWD-BLP4-1600   " SIZE="1765517033472" '
    ' ROTA="0" TYPE="disk"\n'
    'KNAME="sdd" MODEL="NWD-BLP4-1600   " SIZE="1765517033472" '
    ' ROTA="0" TYPE="disk"\n'
    'KNAME="loop0" MODEL="" SIZE="109109248" ROTA="1" TYPE="loop"\n'
    'KNAME="zram0" MODEL="" SIZE="" ROTA="0" TYPE="disk"\n'
    'KNAME="ram0" MODEL="" SIZE="8388608" ROTA="0" TYPE="disk"\n'
    'KNAME="ram1" MODEL="" SIZE="8388608" ROTA="0" TYPE="disk"\n'
    'KNAME="ram2" MODEL="" SIZE="8388608" ROTA="0" TYPE="disk"\n'
    'KNAME="ram3" MODEL="" SIZE="8388608" ROTA="0" TYPE="disk"\n'
    'KNAME="fd1" MODEL="magic" SIZE="4096" ROTA="1" TYPE="disk"\n'
    'KNAME="sdf" MODEL="virtual floppy" SIZE="0" ROTA="1" TYPE="disk"'
)

# NOTE(pas-ha) largest device is 1 byte smaller than 4GiB
BLK_DEVICE_TEMPLATE_SMALL = (
    'KNAME="sda" MODEL="TinyUSB Drive" SIZE="3116853504" '
    'ROTA="0" TYPE="disk"\n'
    'KNAME="sdb" MODEL="AlmostBigEnough Drive" SIZE="4294967295" '
    'ROTA="0" TYPE="disk"'
)
BLK_DEVICE_TEMPLATE_SMALL_DEVICES = [
    hardware.BlockDevice(name='/dev/sda', model='TinyUSB Drive',
                         size=3116853504, rotational=False,
                         vendor="FooTastic"),
    hardware.BlockDevice(name='/dev/sdb', model='AlmostBigEnough Drive',
                         size=4294967295, rotational=False,
                         vendor="FooTastic"),
]

# NOTE(TheJulia): This list intentionally contains duplicates
# as the code filters them out by kernel device name.
# NOTE(dszumski): We include some partitions here to verify that
# they are filtered out when not requested. It is assumed that
# ROTA has been set to 0 on some software RAID devices for testing
# purposes. In practice is appears to inherit from the underyling
# devices, so in this example it would normally be 1.
RAID_BLK_DEVICE_TEMPLATE = (
    'KNAME="sda" MODEL="DRIVE 0" SIZE="1765517033472" '
    'ROTA="1" TYPE="disk"\n'
    'KNAME="sda1" MODEL="DRIVE 0" SIZE="107373133824" '
    'ROTA="1" TYPE="part"\n'
    'KNAME="sdb" MODEL="DRIVE 1" SIZE="1765517033472" '
    'ROTA="1" TYPE="disk"\n'
    'KNAME="sdb" MODEL="DRIVE 1" SIZE="1765517033472" '
    'ROTA="1" TYPE="disk"\n'
    'KNAME="sdb1" MODEL="DRIVE 1" SIZE="107373133824" '
    'ROTA="1" TYPE="part"\n'
    'KNAME="md0p1" MODEL="RAID" SIZE="107236818944" '
    'ROTA="0" TYPE="md"\n'
    'KNAME="md0" MODEL="RAID" SIZE="1765517033470" '
    'ROTA="0" TYPE="raid1"\n'
    'KNAME="md0" MODEL="RAID" SIZE="1765517033470" '
    'ROTA="0" TYPE="raid1"\n'
    'KNAME="md1" MODEL="RAID" SIZE="" ROTA="0" TYPE="raid1"'
)
RAID_BLK_DEVICE_TEMPLATE_DEVICES = [
    hardware.BlockDevice(name='/dev/sda', model='DRIVE 0',
                         size=1765517033472, rotational=True,
                         vendor="FooTastic"),
    hardware.BlockDevice(name='/dev/sdb', model='DRIVE 1',
                         size=1765517033472, rotational=True,
                         vendor="FooTastic"),
    hardware.BlockDevice(name='/dev/md0', model='RAID',
                         size=1765517033470, rotational=False,
                         vendor="FooTastic"),
    hardware.BlockDevice(name='/dev/md1', model='RAID',
                         size=0, rotational=False,
                         vendor="FooTastic"),
]

SHRED_OUTPUT_0_ITERATIONS_ZERO_FALSE = ()

SHRED_OUTPUT_1_ITERATION_ZERO_TRUE = (
    'shred: /dev/sda: pass 1/2 (random)...\n'
    'shred: /dev/sda: pass 1/2 (random)...4.9GiB/29GiB 17%\n'
    'shred: /dev/sda: pass 1/2 (random)...15GiB/29GiB 51%\n'
    'shred: /dev/sda: pass 1/2 (random)...20GiB/29GiB 69%\n'
    'shred: /dev/sda: pass 1/2 (random)...29GiB/29GiB 100%\n'
    'shred: /dev/sda: pass 2/2 (000000)...\n'
    'shred: /dev/sda: pass 2/2 (000000)...4.9GiB/29GiB 17%\n'
    'shred: /dev/sda: pass 2/2 (000000)...15GiB/29GiB 51%\n'
    'shred: /dev/sda: pass 2/2 (000000)...20GiB/29GiB 69%\n'
    'shred: /dev/sda: pass 2/2 (000000)...29GiB/29GiB 100%\n'
)

SHRED_OUTPUT_2_ITERATIONS_ZERO_FALSE = (
    'shred: /dev/sda: pass 1/2 (random)...\n'
    'shred: /dev/sda: pass 1/2 (random)...4.9GiB/29GiB 17%\n'
    'shred: /dev/sda: pass 1/2 (random)...15GiB/29GiB 51%\n'
    'shred: /dev/sda: pass 1/2 (random)...20GiB/29GiB 69%\n'
    'shred: /dev/sda: pass 1/2 (random)...29GiB/29GiB 100%\n'
    'shred: /dev/sda: pass 2/2 (random)...\n'
    'shred: /dev/sda: pass 2/2 (random)...4.9GiB/29GiB 17%\n'
    'shred: /dev/sda: pass 2/2 (random)...15GiB/29GiB 51%\n'
    'shred: /dev/sda: pass 2/2 (random)...20GiB/29GiB 69%\n'
    'shred: /dev/sda: pass 2/2 (random)...29GiB/29GiB 100%\n'
)


LSCPU_OUTPUT = """
Architecture:          x86_64
CPU op-mode(s):        32-bit, 64-bit
Byte Order:            Little Endian
CPU(s):                4
On-line CPU(s) list:   0-3
Thread(s) per core:    1
Core(s) per socket:    4
Socket(s):             1
NUMA node(s):          1
Vendor ID:             GenuineIntel
CPU family:            6
Model:                 45
Model name:            Intel(R) Xeon(R) CPU E5-2609 0 @ 2.40GHz
Stepping:              7
CPU MHz:               1290.000
CPU max MHz:           2400.0000
CPU min MHz:           1200.0000
BogoMIPS:              4800.06
Virtualization:        VT-x
L1d cache:             32K
L1i cache:             32K
L2 cache:              256K
L3 cache:              10240K
NUMA node0 CPU(s):     0-3
"""

LSCPU_OUTPUT_NO_MAX_MHZ = """
Architecture:          x86_64
CPU op-mode(s):        32-bit, 64-bit
Byte Order:            Little Endian
CPU(s):                12
On-line CPU(s) list:   0-11
Thread(s) per core:    2
Core(s) per socket:    6
Socket(s):             1
NUMA node(s):          1
Vendor ID:             GenuineIntel
CPU family:            6
Model:                 63
Model name:            Intel(R) Xeon(R) CPU E5-1650 v3 @ 3.50GHz
Stepping:              2
CPU MHz:               1794.433
BogoMIPS:              6983.57
Virtualization:        VT-x
L1d cache:             32K
L1i cache:             32K
L2 cache:              256K
L3 cache:              15360K
NUMA node0 CPU(s):     0-11
"""

# NOTE(dtanstur): flags list stripped down for sanity reasons
CPUINFO_FLAGS_OUTPUT = """
flags           : fpu vme de pse
"""

LSHW_JSON_OUTPUT_V1 = ("""
{
  "id": "fuzzypickles",
  "product": "ABC123 (GENERIC_SERVER)",
  "vendor": "GENERIC",
  "serial": "1234567",
  "width": 64,
  "capabilities": {
    "smbios-2.7": "SMBIOS version 2.7",
    "dmi-2.7": "DMI version 2.7",
    "vsyscall32": "32-bit processes"
  },
  "children": [
    {
      "id": "core",
      "description": "Motherboard",
      "product": "ABC123",
      "vendor": "GENERIC",
      "serial": "ABCDEFGHIJK",
      "children": [
        {
          "id": "memory",
          "class": "memory",
          "description": "System Memory",
          "units": "bytes",
          "size": 4294967296,
          "children": [
            {
              "id": "bank:0",
              "class": "memory",
              "physid": "0",
              "units": "bytes",
              "size": 2147483648,
              "width": 64,
              "clock": 1600000000
            },
            {
              "id": "bank:1",
              "class": "memory",
              "physid": "1"
            },
            {
              "id": "bank:2",
              "class": "memory",
              "physid": "2",
              "units": "bytes",
              "size": 1073741824,
              "width": 64,
              "clock": 1600000000
            },
            {
              "id": "bank:3",
              "class": "memory",
              "physid": "3",
              "units": "bytes",
              "size": 1073741824,
              "width": 64,
              "clock": 1600000000
            }
          ]
        },
        {
          "id": "cpu:0",
          "class": "processor",
          "claimed": true,
          "product": "Intel Xeon E312xx (Sandy Bridge)",
          "vendor": "Intel Corp.",
          "physid": "1",
          "businfo": "cpu@0",
          "width": 64,
          "capabilities": {
            "fpu": "mathematical co-processor",
            "fpu_exception": "FPU exceptions reporting",
            "wp": true,
            "mmx": "multimedia extensions (MMX)"
          }
        }
      ]
    },
    {
      "id": "network:0",
      "class": "network",
      "claimed": true,
      "description": "Ethernet interface",
      "physid": "1",
      "logicalname": "ovs-tap",
      "serial": "1c:90:c0:f9:4e:a1",
      "units": "bit/s",
      "size": 10000000000,
      "configuration": {
        "autonegotiation": "off",
        "broadcast": "yes",
        "driver": "veth",
        "driverversion": "1.0",
        "duplex": "full",
        "link": "yes",
        "multicast": "yes",
        "port": "twisted pair",
        "speed": "10Gbit/s"
      },
      "capabilities": {
        "ethernet": true,
        "physical": "Physical interface"
      }
    }
  ]
}
""", "")

LSHW_JSON_OUTPUT_V2 = ("""
{
  "id" : "bumblebee",
  "class" : "system",
  "claimed" : true,
  "handle" : "DMI:0001",
  "description" : "Rack Mount Chassis",
  "product" : "ABCD",
  "vendor" : "ABCD",
  "version" : "1234",
  "serial" : "1234",
  "width" : 64,
  "configuration" : {
    "boot" : "normal",
    "chassis" : "rackmount",
    "family" : "Intel Grantley EP",
    "sku" : "NULL",
    "uuid" : "00010002-0003-0004-0005-000600070008"
  },
  "capabilities" : {
    "smbios-2.8" : "SMBIOS version 2.8",
    "dmi-2.7" : "DMI version 2.7",
    "vsyscall32" : "32-bit processes"
  },
  "children" : [
    {
      "id" : "core",
      "class" : "bus",
      "claimed" : true,
      "handle" : "DMI:0002",
      "description" : "Motherboard",
      "product" : "ABCD",
      "vendor" : "ABCD",
      "physid" : "0",
      "version" : "1234",
      "serial" : "1234",
      "slot" : "NULL",
      "children" : [
        {
          "id" : "memory:0",
          "class" : "memory",
          "claimed" : true,
          "handle" : "DMI:004A",
          "description" : "System Memory",
          "physid" : "4a",
          "slot" : "System board or motherboard",
          "children" : [
            {
              "id" : "bank:0",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:004C",
              "description" : "DIMM Synchronous 2133 MHz (0.5 ns)",
              "product" : "36ASF2G72PZ-2G1A2",
              "vendor" : "Micron",
              "physid" : "0",
              "serial" : "101B6543",
              "slot" : "DIMM_A0",
              "units" : "bytes",
              "size" : 17179869184,
              "width" : 64,
              "clock" : 2133000000
            },
            {
              "id" : "bank:1",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:004E",
              "description" : "DIMM Synchronous [empty]",
              "product" : "NO DIMM",
              "vendor" : "NO DIMM",
              "physid" : "1",
              "serial" : "NO DIMM",
              "slot" : "DIMM_A1"
            },
            {
              "id" : "bank:2",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:004F",
              "description" : "DIMM Synchronous 2133 MHz (0.5 ns)",
              "product" : "36ASF2G72PZ-2G1A2",
              "vendor" : "Micron",
              "physid" : "2",
              "serial" : "101B654E",
              "slot" : "DIMM_A2",
              "units" : "bytes",
              "size" : 17179869184,
              "width" : 64,
              "clock" : 2133000000
            },
            {
              "id" : "bank:3",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:0051",
              "description" : "DIMM Synchronous [empty]",
              "product" : "NO DIMM",
              "vendor" : "NO DIMM",
              "physid" : "3",
              "serial" : "NO DIMM",
              "slot" : "DIMM_A3"
            }
          ]
        },
        {
          "id" : "memory:1",
          "class" : "memory",
          "claimed" : true,
          "handle" : "DMI:0052",
          "description" : "System Memory",
          "physid" : "52",
          "slot" : "System board or motherboard",
          "children" : [
            {
              "id" : "bank:0",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:0054",
              "description" : "DIMM Synchronous 2133 MHz (0.5 ns)",
              "product" : "36ASF2G72PZ-2G1A2",
              "vendor" : "Micron",
              "physid" : "0",
              "serial" : "101B6545",
              "slot" : "DIMM_A4",
              "units" : "bytes",
              "size" : 17179869184,
              "width" : 64,
              "clock" : 2133000000
            },
            {
              "id" : "bank:1",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:0056",
              "description" : "DIMM Synchronous [empty]",
              "product" : "NO DIMM",
              "vendor" : "NO DIMM",
              "physid" : "1",
              "serial" : "NO DIMM",
              "slot" : "DIMM_A5"
            },
            {
              "id" : "bank:2",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:0057",
              "description" : "DIMM Synchronous 2133 MHz (0.5 ns)",
              "product" : "36ASF2G72PZ-2G1A2",
              "vendor" : "Micron",
              "physid" : "2",
              "serial" : "101B6540",
              "slot" : "DIMM_A6",
              "units" : "bytes",
              "size" : 17179869184,
              "width" : 64,
              "clock" : 2133000000
            },
            {
              "id" : "bank:3",
              "class" : "memory",
              "claimed" : true,
              "handle" : "DMI:0059",
              "description" : "DIMM Synchronous [empty]",
              "product" : "NO DIMM",
              "vendor" : "NO DIMM",
              "physid" : "3",
              "serial" : "NO DIMM",
              "slot" : "DIMM_A7"
            }
          ]
        },
        {
          "id" : "memory:4",
          "class" : "memory",
          "physid" : "1"
        },
        {
          "id" : "memory:5",
          "class" : "memory",
          "physid" : "2"
        }
      ]
    }
  ]
}
""", "")

LSHW_JSON_OUTPUT_ARM64 = ("""
{
  "id" : "debian",
  "class" : "system",
  "claimed" : true,
  "description" : "Computer",
  "width" : 64,
  "capabilities" : {
    "cp15_barrier" : true,
    "setend" : true,
    "swp" : true
  },
  "children" : [
    {
      "id" : "core",
      "class" : "bus",
      "claimed" : true,
      "description" : "Motherboard",
      "physid" : "0",
      "children" : [
        {
          "id" : "memory",
          "class" : "memory",
          "claimed" : true,
          "description" : "System memory",
          "physid" : "0",
          "units" : "bytes",
          "size" : 4143972352
        },
        {
          "id" : "cpu:0",
          "class" : "processor",
          "claimed" : true,
          "physid" : "1",
          "businfo" : "cpu@0",
          "capabilities" : {
            "fp" : "Floating point instructions",
            "asimd" : "Advanced SIMD",
            "evtstrm" : "Event stream",
            "aes" : "AES instructions",
            "pmull" : "PMULL instruction",
            "sha1" : "SHA1 instructions",
            "sha2" : "SHA2 instructions",
            "crc32" : "CRC extension",
            "cpuid" : true
          }
        },
        {
          "id" : "pci:0",
          "class" : "bridge",
          "claimed" : true,
          "handle" : "PCIBUS:0002:e9",
          "physid" : "100",
          "businfo" : "pci@0002:e8:00.0",
          "version" : "01",
          "width" : 32,
          "clock" : 33000000,
          "configuration" : {
            "driver" : "pcieport"
          },
          "capabilities" : {
            "pci" : true,
            "pm" : "Power Management",
            "msi" : "Message Signalled Interrupts",
            "pciexpress" : "PCI Express",
            "bus_master" : "bus mastering",
            "cap_list" : "PCI capabilities listing"
          }
        }
      ]
    },
    {
      "id" : "network:0",
      "class" : "network",
      "claimed" : true,
      "description" : "Ethernet interface",
      "physid" : "2",
      "logicalname" : "enahisic2i2",
      "serial" : "d0:ef:c1:e9:bf:33",
      "configuration" : {
        "autonegotiation" : "off",
        "broadcast" : "yes",
        "driver" : "hns",
        "driverversion" : "2.0",
        "firmware" : "N/A",
        "link" : "no",
        "multicast" : "yes",
        "port" : "fibre"
      },
      "capabilities" : {
        "ethernet" : true,
        "physical" : "Physical interface",
        "fibre" : "optical fibre"
      }
    }
  ]
}
""", "")

SMARTCTL_NORMAL_OUTPUT = ("""
smartctl 6.2 2017-02-27 r4394 [x86_64-linux-3.10.0-693.21.1.el7.x86_64] (local build)
Copyright (C) 2002-13, Bruce Allen, Christian Franke, www.smartmontools.org

ATA Security is:  Disabled, NOT FROZEN [SEC1]
""")  # noqa

SMARTCTL_UNAVAILABLE_OUTPUT = ("""
smartctl 6.2 2017-02-27 r4394 [x86_64-linux-3.10.0-693.21.1.el7.x86_64] (local build)
Copyright (C) 2002-13, Bruce Allen, Christian Franke, www.smartmontools.org

ATA Security is:  Unavailable
""")  # noqa


IPMITOOL_LAN6_PRINT_DYNAMIC_ADDR = """
IPv6 Dynamic Address 0:
    Source/Type:    DHCPv6
    Address:        2001:1234:1234:1234:1234:1234:1234:1234/64
    Status:         active
IPv6 Dynamic Address 1:
    Source/Type:    DHCPv6
    Address:        ::/0
    Status:         active
IPv6 Dynamic Address 2:
    Source/Type:    DHCPv6
    Address:        ::/0
    Status:         active
"""

IPMITOOL_LAN6_PRINT_STATIC_ADDR = """
IPv6 Static Address 0:
    Enabled:        yes
    Address:        2001:5678:5678:5678:5678:5678:5678:5678/64
    Status:         active
IPv6 Static Address 1:
    Enabled:        no
    Address:        ::/0
    Status:         disabled
IPv6 Static Address 2:
    Enabled:        no
    Address:        ::/0
    Status:         disabled
"""

MDADM_DETAIL_OUTPUT = ("""/dev/md0:
           Version : 1.0
     Creation Time : Fri Feb 15 12:37:44 2019
        Raid Level : raid1
        Array Size : 1048512 (1023.94 MiB 1073.68 MB)
     Used Dev Size : 1048512 (1023.94 MiB 1073.68 MB)
      Raid Devices : 2
     Total Devices : 2
       Persistence : Superblock is persistent

       Update Time : Fri Feb 15 12:38:02 2019
             State : clean
    Active Devices : 2
   Working Devices : 2
    Failed Devices : 0
     Spare Devices : 0

Consistency Policy : resync

              Name : abc.xyz.com:0  (local to host abc.xyz.com)
              UUID : 83143055:2781ddf5:2c8f44c7:9b45d92e
            Events : 17

    Number   Major   Minor   RaidDevice State
       0     253       64        0      active sync   /dev/vde1
       1     253       80        1      active sync   /dev/vdf1
""")

MDADM_DETAIL_OUTPUT_NVME = ("""/dev/md0:
        Version : 1.2
  Creation Time : Wed Aug  7 13:47:27 2019
     Raid Level : raid1
     Array Size : 439221248 (418.87 GiB 449.76 GB)
  Used Dev Size : 439221248 (418.87 GiB 449.76 GB)
   Raid Devices : 2
  Total Devices : 2
    Persistence : Superblock is persistent

  Intent Bitmap : Internal

    Update Time : Wed Aug  7 14:37:21 2019
          State : clean
 Active Devices : 2
Working Devices : 2
 Failed Devices : 0
  Spare Devices : 0

           Name : rescue:0  (local to host rescue)
           UUID : abe222bc:98735860:ab324674:e4076313
         Events : 426

    Number   Major   Minor   RaidDevice State
       0     259        2        0      active sync   /dev/nvme0n1p1
       1     259        3        1      active sync   /dev/nvme1n1p1
""")


MDADM_DETAIL_OUTPUT_BROKEN_RAID0 = ("""/dev/md126:
           Version : 1.2
        Raid Level : raid0
     Total Devices : 1
       Persistence : Superblock is persistent

             State : inactive
   Working Devices : 1

              Name : prj6ogxgyzd:1
              UUID : b5e136c0:a7e379b7:db25e45d:4b63928b
            Events : 0

    Number   Major   Minor   RaidDevice

       -       8        2        -        /dev/sda2
""")


class FakeHardwareManager(hardware.GenericHardwareManager):
    def __init__(self, hardware_support):
        self._hardware_support = hardware_support

    def evaluate_hardware_support(self):
        return self._hardware_support


class TestHardwareManagerLoading(base.IronicAgentTest):
    def setUp(self):
        super(TestHardwareManagerLoading, self).setUp()
        # In order to use ExtensionManager.make_test_instance() without
        # creating a new only-for-test codepath, we instantiate the test
        # instance outside of the test case in setUp, where we can access
        # make_test_instance() before it gets mocked. Inside of the test case
        # we set this as the return value of the mocked constructor, so we can
        # verify that the constructor is called correctly while still using a
        # more realistic ExtensionManager
        fake_ep = mock.Mock()
        fake_ep.module_name = 'fake'
        fake_ep.attrs = ['fake attrs']
        ext1 = extension.Extension(
            'fake_generic0', fake_ep, None,
            FakeHardwareManager(hardware.HardwareSupport.GENERIC))
        ext2 = extension.Extension(
            'fake_mainline0', fake_ep, None,
            FakeHardwareManager(hardware.HardwareSupport.MAINLINE))
        ext3 = extension.Extension(
            'fake_generic1', fake_ep, None,
            FakeHardwareManager(hardware.HardwareSupport.GENERIC))
        self.correct_hw_manager = ext2.obj
        self.fake_ext_mgr = extension.ExtensionManager.make_test_instance([
            ext1, ext2, ext3
        ])


@mock.patch.object(hardware, '_udev_settle', lambda *_: None)
class TestGenericHardwareManager(base.IronicAgentTest):
    def setUp(self):
        super(TestGenericHardwareManager, self).setUp()
        self.hardware = hardware.GenericHardwareManager()
        self.node = {'uuid': 'dda135fb-732d-4742-8e72-df8f3199d244',
                     'driver_internal_info': {}}
        CONF.clear_override('disk_wait_attempts')
        CONF.clear_override('disk_wait_delay')

    def test_get_clean_steps(self):
        expected_clean_steps = [
            {
                'step': 'erase_devices',
                'priority': 10,
                'interface': 'deploy',
                'reboot_requested': False,
                'abortable': True
            },
            {
                'step': 'erase_devices_metadata',
                'priority': 99,
                'interface': 'deploy',
                'reboot_requested': False,
                'abortable': True
            },
            {
                'step': 'delete_configuration',
                'priority': 0,
                'interface': 'raid',
                'reboot_requested': False,
                'abortable': True
            },
            {
                'step': 'create_configuration',
                'priority': 0,
                'interface': 'raid',
                'reboot_requested': False,
                'abortable': True
            }
        ]
        clean_steps = self.hardware.get_clean_steps(self.node, [])
        self.assertEqual(expected_clean_steps, clean_steps)

    @mock.patch('binascii.hexlify', autospec=True)
    @mock.patch('ironic_python_agent.netutils.get_lldp_info', autospec=True)
    def test_collect_lldp_data(self, mock_lldp_info, mock_hexlify):
        if_names = ['eth0', 'lo']
        mock_lldp_info.return_value = {if_names[0]: [
            (0, b''),
            (1, b'foo\x01'),
            (2, b'\x02bar')],
        }
        mock_hexlify.side_effect = [
            b'',
            b'666f6f01',
            b'02626172'
        ]
        expected_lldp_data = {
            'eth0': [
                (0, ''),
                (1, '666f6f01'),
                (2, '02626172')],
        }
        result = self.hardware.collect_lldp_data(if_names)
        self.assertIn(if_names[0], result)
        self.assertEqual(expected_lldp_data, result)

    @mock.patch('ironic_python_agent.netutils.get_lldp_info', autospec=True)
    def test_collect_lldp_data_netutils_exception(self, mock_lldp_info):
        if_names = ['eth0', 'lo']
        mock_lldp_info.side_effect = Exception('fake error')
        result = self.hardware.collect_lldp_data(if_names)
        expected_lldp_data = {}
        self.assertEqual(expected_lldp_data, result)

    @mock.patch.object(hardware, 'LOG', autospec=True)
    @mock.patch('binascii.hexlify', autospec=True)
    @mock.patch('ironic_python_agent.netutils.get_lldp_info', autospec=True)
    def test_collect_lldp_data_decode_exception(self, mock_lldp_info,
                                                mock_hexlify, mock_log):
        if_names = ['eth0', 'lo']
        mock_lldp_info.return_value = {if_names[0]: [
            (0, b''),
            (1, b'foo\x01'),
            (2, b'\x02bar')],
        }
        mock_hexlify.side_effect = [
            b'',
            b'666f6f01',
            binascii.Error('fake_error')
        ]
        expected_lldp_data = {
            'eth0': [
                (0, ''),
                (1, '666f6f01')],
        }
        result = self.hardware.collect_lldp_data(if_names)
        mock_log.warning.assert_called_once()
        self.assertIn(if_names[0], result)
        self.assertEqual(expected_lldp_data, result)

    @mock.patch('ironic_python_agent.hardware._get_managers', autospec=True)
    @mock.patch('netifaces.ifaddresses', autospec=True)
    @mock.patch('os.listdir', autospec=True)
    @mock.patch('os.path.exists', autospec=True)
    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(netutils, 'get_mac_addr', autospec=True)
    @mock.patch.object(netutils, 'interface_has_carrier', autospec=True)
    def test_list_network_interfaces(self,
                                     mock_has_carrier,
                                     mock_get_mac,
                                     mocked_execute,
                                     mocked_open,
                                     mocked_exists,
                                     mocked_listdir,
                                     mocked_ifaddresses,
                                     mocked_get_managers):
        mocked_get_managers.return_value = [hardware.GenericHardwareManager()]
        mocked_listdir.return_value = ['lo', 'eth0']
        mocked_exists.side_effect = [False, True]
        mocked_open.return_value.__enter__ = lambda s: s
        mocked_open.return_value.__exit__ = mock.Mock()
        read_mock = mocked_open.return_value.read
        read_mock.side_effect = ['1']
        mocked_ifaddresses.return_value = {
            netifaces.AF_INET: [{'addr': '192.168.1.2'}],
            netifaces.AF_INET6: [{'addr': 'fd00::101'}]
        }
        mocked_execute.return_value = ('em0\n', '')
        mock_get_mac.mock_has_carrier = True
        mock_get_mac.return_value = '00:0c:29:8c:11:b1'
        interfaces = self.hardware.list_network_interfaces()
        self.assertEqual(1, len(interfaces))
        self.assertEqual('eth0', interfaces[0].name)
        self.assertEqual('00:0c:29:8c:11:b1', interfaces[0].mac_address)
        self.assertEqual('192.168.1.2', interfaces[0].ipv4_address)
        self.assertEqual('fd00::101', interfaces[0].ipv6_address)
        self.assertIsNone(interfaces[0].lldp)
        self.assertTrue(interfaces[0].has_carrier)
        self.assertEqual('em0', interfaces[0].biosdevname)

    @mock.patch('ironic_python_agent.hardware._get_managers', autospec=True)
    @mock.patch('netifaces.ifaddresses', autospec=True)
    @mock.patch('os.listdir', autospec=True)
    @mock.patch('os.path.exists', autospec=True)
    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(netutils, 'get_mac_addr', autospec=True)
    @mock.patch.object(netutils, 'interface_has_carrier', autospec=True)
    def test_list_network_interfaces_with_biosdevname(self,
                                                      mock_has_carrier,
                                                      mock_get_mac,
                                                      mocked_execute,
                                                      mocked_open,
                                                      mocked_exists,
                                                      mocked_listdir,
                                                      mocked_ifaddresses,
                                                      mocked_get_managers):
        mocked_get_managers.return_value = [hardware.GenericHardwareManager()]
        mocked_listdir.return_value = ['lo', 'eth0']
        mocked_exists.side_effect = [False, True]
        mocked_open.return_value.__enter__ = lambda s: s
        mocked_open.return_value.__exit__ = mock.Mock()
        read_mock = mocked_open.return_value.read
        read_mock.side_effect = ['1']
        mocked_ifaddresses.return_value = {
            netifaces.AF_INET: [{'addr': '192.168.1.2'}],
            netifaces.AF_INET6: [{'addr': 'fd00::101'}]
        }
        mocked_execute.return_value = ('em0\n', '')
        mock_get_mac.return_value = '00:0c:29:8c:11:b1'
        mock_has_carrier.return_value = True
        interfaces = self.hardware.list_network_interfaces()
        self.assertEqual(1, len(interfaces))
        self.assertEqual('eth0', interfaces[0].name)
        self.assertEqual('00:0c:29:8c:11:b1', interfaces[0].mac_address)
        self.assertEqual('192.168.1.2', interfaces[0].ipv4_address)
        self.assertEqual('fd00::101', interfaces[0].ipv6_address)
        self.assertIsNone(interfaces[0].lldp)
        self.assertTrue(interfaces[0].has_carrier)
        self.assertEqual('em0', interfaces[0].biosdevname)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bios_given_nic_name_ok(self, mock_execute):
        interface_name = 'eth0'
        mock_execute.return_value = ('em0\n', '')
        result = self.hardware.get_bios_given_nic_name(interface_name)
        self.assertEqual('em0', result)
        mock_execute.assert_called_once_with('biosdevname', '-i',
                                             interface_name)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bios_given_nic_name_oserror(self, mock_execute):
        interface_name = 'eth0'
        mock_execute.side_effect = OSError()
        result = self.hardware.get_bios_given_nic_name(interface_name)
        self.assertIsNone(result)
        mock_execute.assert_called_once_with('biosdevname', '-i',
                                             interface_name)

    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(hardware, 'LOG', autospec=True)
    def test_get_bios_given_nic_name_process_exec_err4(self, mock_log,
                                                       mock_execute):
        interface_name = 'eth0'
        mock_execute.side_effect = [
            processutils.ProcessExecutionError(exit_code=4)]

        result = self.hardware.get_bios_given_nic_name(interface_name)

        mock_log.info.assert_called_once_with(
            'The system is a virtual machine, so biosdevname utility does '
            'not provide names for virtual NICs.')
        self.assertIsNone(result)
        mock_execute.assert_called_once_with('biosdevname', '-i',
                                             interface_name)

    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(hardware, 'LOG', autospec=True)
    def test_get_bios_given_nic_name_process_exec_err3(self, mock_log,
                                                       mock_execute):
        interface_name = 'eth0'
        mock_execute.side_effect = [
            processutils.ProcessExecutionError(exit_code=3)]

        result = self.hardware.get_bios_given_nic_name(interface_name)

        mock_log.warning.assert_called_once_with(
            'Biosdevname returned exit code %s', 3)
        self.assertIsNone(result)
        mock_execute.assert_called_once_with('biosdevname', '-i',
                                             interface_name)

    @mock.patch('ironic_python_agent.hardware._get_managers', autospec=True)
    @mock.patch('ironic_python_agent.netutils.get_lldp_info', autospec=True)
    @mock.patch('netifaces.ifaddresses', autospec=True)
    @mock.patch('os.listdir', autospec=True)
    @mock.patch('os.path.exists', autospec=True)
    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(netutils, 'get_mac_addr', autospec=True)
    @mock.patch.object(netutils, 'interface_has_carrier', autospec=True)
    def test_list_network_interfaces_with_lldp(self,
                                               mock_has_carrier,
                                               mock_get_mac,
                                               mocked_execute,
                                               mocked_open,
                                               mocked_exists,
                                               mocked_listdir,
                                               mocked_ifaddresses,
                                               mocked_lldp_info,
                                               mocked_get_managers):
        mocked_get_managers.return_value = [hardware.GenericHardwareManager()]
        CONF.set_override('collect_lldp', True)
        mocked_listdir.return_value = ['lo', 'eth0']
        mocked_exists.side_effect = [False, True]
        mocked_open.return_value.__enter__ = lambda s: s
        mocked_open.return_value.__exit__ = mock.Mock()
        read_mock = mocked_open.return_value.read
        read_mock.side_effect = ['1']
        mocked_ifaddresses.return_value = {
            netifaces.AF_INET: [{'addr': '192.168.1.2'}],
            netifaces.AF_INET6: [{'addr': 'fd00::101'}]
        }
        mocked_lldp_info.return_value = {'eth0': [
            (0, b''),
            (1, b'\x04\x88Z\x92\xecTY'),
            (2, b'\x05Ethernet1/18'),
            (3, b'\x00x')]
        }
        mock_has_carrier.return_value = True
        mock_get_mac.return_value = '00:0c:29:8c:11:b1'
        mocked_execute.return_value = ('em0\n', '')
        interfaces = self.hardware.list_network_interfaces()
        self.assertEqual(1, len(interfaces))
        self.assertEqual('eth0', interfaces[0].name)
        self.assertEqual('00:0c:29:8c:11:b1', interfaces[0].mac_address)
        self.assertEqual('192.168.1.2', interfaces[0].ipv4_address)
        self.assertEqual('fd00::101', interfaces[0].ipv6_address)
        expected_lldp_info = [
            (0, ''),
            (1, '04885a92ec5459'),
            (2, '0545746865726e6574312f3138'),
            (3, '0078'),
        ]
        self.assertEqual(expected_lldp_info, interfaces[0].lldp)
        self.assertTrue(interfaces[0].has_carrier)
        self.assertEqual('em0', interfaces[0].biosdevname)

    @mock.patch.object(netutils, 'interface_has_carrier', autospec=True)
    @mock.patch.object(netutils, 'get_mac_addr', autospec=True)
    @mock.patch('ironic_python_agent.hardware._get_managers', autospec=True)
    @mock.patch('ironic_python_agent.netutils.get_lldp_info', autospec=True)
    @mock.patch('netifaces.ifaddresses', autospec=True)
    @mock.patch('os.listdir', autospec=True)
    @mock.patch('os.path.exists', autospec=True)
    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_list_network_interfaces_with_lldp_error(
            self, mocked_execute, mocked_open, mocked_exists, mocked_listdir,
            mocked_ifaddresses, mocked_lldp_info, mocked_get_managers,
            mock_get_mac, mock_has_carrier):
        mocked_get_managers.return_value = [hardware.GenericHardwareManager()]
        CONF.set_override('collect_lldp', True)
        mocked_listdir.return_value = ['lo', 'eth0']
        mocked_exists.side_effect = [False, True]
        mocked_open.return_value.__enter__ = lambda s: s
        mocked_open.return_value.__exit__ = mock.Mock()
        read_mock = mocked_open.return_value.read
        read_mock.side_effect = ['1']
        mocked_ifaddresses.return_value = {
            netifaces.AF_INET: [{'addr': '192.168.1.2'}],
            netifaces.AF_INET6: [{'addr': 'fd00::101'}]
        }
        mocked_lldp_info.side_effect = Exception('Boom!')
        mocked_execute.return_value = ('em0\n', '')
        mock_has_carrier.return_value = True
        mock_get_mac.return_value = '00:0c:29:8c:11:b1'
        interfaces = self.hardware.list_network_interfaces()
        self.assertEqual(1, len(interfaces))
        self.assertEqual('eth0', interfaces[0].name)
        self.assertEqual('00:0c:29:8c:11:b1', interfaces[0].mac_address)
        self.assertEqual('192.168.1.2', interfaces[0].ipv4_address)
        self.assertEqual('fd00::101', interfaces[0].ipv6_address)
        self.assertIsNone(interfaces[0].lldp)
        self.assertTrue(interfaces[0].has_carrier)
        self.assertEqual('em0', interfaces[0].biosdevname)

    @mock.patch('ironic_python_agent.hardware._get_managers', autospec=True)
    @mock.patch('netifaces.ifaddresses', autospec=True)
    @mock.patch('os.listdir', autospec=True)
    @mock.patch('os.path.exists', autospec=True)
    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(netutils, 'get_mac_addr', autospec=True)
    @mock.patch.object(netutils, 'interface_has_carrier', autospec=True)
    def test_list_network_interfaces_no_carrier(self,
                                                mock_has_carrier,
                                                mock_get_mac,
                                                mocked_execute,
                                                mocked_open,
                                                mocked_exists,
                                                mocked_listdir,
                                                mocked_ifaddresses,
                                                mocked_get_managers):

        mocked_get_managers.return_value = [hardware.GenericHardwareManager()]
        mocked_listdir.return_value = ['lo', 'eth0']
        mocked_exists.side_effect = [False, True]
        mocked_open.return_value.__enter__ = lambda s: s
        mocked_open.return_value.__exit__ = mock.Mock()
        read_mock = mocked_open.return_value.read
        read_mock.side_effect = [OSError('boom')]
        mocked_ifaddresses.return_value = {
            netifaces.AF_INET: [{'addr': '192.168.1.2'}],
            netifaces.AF_INET6: [{'addr': 'fd00::101'}]
        }
        mocked_execute.return_value = ('em0\n', '')
        mock_has_carrier.return_value = False
        mock_get_mac.return_value = '00:0c:29:8c:11:b1'
        interfaces = self.hardware.list_network_interfaces()
        self.assertEqual(1, len(interfaces))
        self.assertEqual('eth0', interfaces[0].name)
        self.assertEqual('00:0c:29:8c:11:b1', interfaces[0].mac_address)
        self.assertEqual('192.168.1.2', interfaces[0].ipv4_address)
        self.assertEqual('fd00::101', interfaces[0].ipv6_address)
        self.assertFalse(interfaces[0].has_carrier)
        self.assertIsNone(interfaces[0].vendor)
        self.assertEqual('em0', interfaces[0].biosdevname)

    @mock.patch('ironic_python_agent.hardware._get_managers', autospec=True)
    @mock.patch('netifaces.ifaddresses', autospec=True)
    @mock.patch('os.listdir', autospec=True)
    @mock.patch('os.path.exists', autospec=True)
    @mock.patch('six.moves.builtins.open', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(netutils, 'get_mac_addr', autospec=True)
    @mock.patch.object(netutils, 'interface_has_carrier', autospec=True)
    def test_list_network_interfaces_with_vendor_info(self,
                                                      mock_has_carrier,
                                                      mock_get_mac,
                                                      mocked_execute,
                                                      mocked_open,
                                                      mocked_exists,
                                                      mocked_listdir,
                                                      mocked_ifaddresses,
                                                      mocked_get_managers):
        mocked_get_managers.return_value = [hardware.GenericHardwareManager()]
        mocked_listdir.return_value = ['lo', 'eth0']
        mocked_exists.side_effect = [False, True]
        mocked_open.return_value.__enter__ = lambda s: s
        mocked_open.return_value.__exit__ = mock.Mock()
        read_mock = mocked_open.return_value.read
        mac = '00:0c:29:8c:11:b1'
        read_mock.side_effect = ['0x15b3\n', '0x1014\n']
        mocked_ifaddresses.return_value = {
            netifaces.AF_INET: [{'addr': '192.168.1.2'}],
            netifaces.AF_INET6: [{'addr': 'fd00::101'}]
        }
        mocked_execute.return_value = ('em0\n', '')
        mock_has_carrier.return_value = True
        mock_get_mac.return_value = mac
        interfaces = self.hardware.list_network_interfaces()
        self.assertEqual(1, len(interfaces))
        self.assertEqual('eth0', interfaces[0].name)
        self.assertEqual(mac, interfaces[0].mac_address)
        self.assertEqual('192.168.1.2', interfaces[0].ipv4_address)
        self.assertEqual('fd00::101', interfaces[0].ipv6_address)
        self.assertTrue(interfaces[0].has_carrier)
        self.assertEqual('0x15b3', interfaces[0].vendor)
        self.assertEqual('0x1014', interfaces[0].product)
        self.assertEqual('em0', interfaces[0].biosdevname)

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(hardware, 'get_cached_node', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_os_install_device(self, mocked_execute, mock_cached_node,
                                   mocked_listdir, mocked_readlink):
        mocked_readlink.return_value = '../../sda'
        mocked_listdir.return_value = ['1:0:0:0']
        mock_cached_node.return_value = None
        mocked_execute.return_value = (BLK_DEVICE_TEMPLATE, '')
        self.assertEqual('/dev/sdb', self.hardware.get_os_install_device())
        mocked_execute.assert_called_once_with(
            'lsblk', '-Pbia', '-oKNAME,MODEL,SIZE,ROTA,TYPE',
            check_exit_code=[0])
        mock_cached_node.assert_called_once_with()

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(hardware, 'get_cached_node', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_os_install_device_raid(self, mocked_execute,
                                        mock_cached_node, mocked_listdir,
                                        mocked_readlink):
        # NOTE(TheJulia): The readlink and listdir mocks are just to satisfy
        # what is functionally an available path check and that information
        # is stored in the returned result for use by root device hints.
        mocked_readlink.side_effect = '../../sda'
        mocked_listdir.return_value = ['1:0:0:0']
        mock_cached_node.return_value = None
        mocked_execute.return_value = (RAID_BLK_DEVICE_TEMPLATE, '')
        # This should ideally select the smallest device and in theory raid
        # should always be smaller
        self.assertEqual('/dev/md0', self.hardware.get_os_install_device())
        mocked_execute.assert_called_once_with(
            'lsblk', '-Pbia', '-oKNAME,MODEL,SIZE,ROTA,TYPE',
            check_exit_code=[0])
        mock_cached_node.assert_called_once_with()

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(hardware, 'get_cached_node', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_os_install_device_fails(self, mocked_execute,
                                         mock_cached_node,
                                         mocked_listdir, mocked_readlink):
        """Fail to find device >=4GB w/o root device hints"""
        mocked_readlink.return_value = '../../sda'
        mocked_listdir.return_value = ['1:0:0:0']
        mock_cached_node.return_value = None
        mocked_execute.return_value = (BLK_DEVICE_TEMPLATE_SMALL, '')
        ex = self.assertRaises(errors.DeviceNotFound,
                               self.hardware.get_os_install_device)
        mocked_execute.assert_called_once_with(
            'lsblk', '-Pbia', '-oKNAME,MODEL,SIZE,ROTA,TYPE',
            check_exit_code=[0])
        self.assertIn(str(4 * units.Gi), ex.details)
        mock_cached_node.assert_called_once_with()

    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    @mock.patch.object(hardware, 'get_cached_node', autospec=True)
    def _get_os_install_device_root_device_hints(self, hints, expected_device,
                                                 mock_cached_node, mock_dev):
        mock_cached_node.return_value = {'properties': {'root_device': hints},
                                         'uuid': 'node1'}
        model = 'fastable sd131 7'
        mock_dev.return_value = [
            hardware.BlockDevice(name='/dev/sda',
                                 model='TinyUSB Drive',
                                 size=3116853504,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 wwn='wwn0',
                                 wwn_with_extension='wwn0ven0',
                                 wwn_vendor_extension='ven0',
                                 serial='serial0'),
            hardware.BlockDevice(name='/dev/sdb',
                                 model=model,
                                 size=10737418240,
                                 rotational=True,
                                 vendor='fake-vendor',
                                 wwn='fake-wwn',
                                 wwn_with_extension='fake-wwnven0',
                                 wwn_vendor_extension='ven0',
                                 serial='fake-serial',
                                 by_path='/dev/disk/by-path/1:0:0:0'),
        ]

        self.assertEqual(expected_device,
                         self.hardware.get_os_install_device())
        mock_cached_node.assert_called_once_with()
        mock_dev.assert_called_once_with()

    def test_get_os_install_device_root_device_hints_model(self):
        self._get_os_install_device_root_device_hints(
            {'model': 'fastable sd131 7'}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_wwn(self):
        self._get_os_install_device_root_device_hints(
            {'wwn': 'wwn0'}, '/dev/sda')

    def test_get_os_install_device_root_device_hints_serial(self):
        self._get_os_install_device_root_device_hints(
            {'serial': 'serial0'}, '/dev/sda')

    def test_get_os_install_device_root_device_hints_size(self):
        self._get_os_install_device_root_device_hints(
            {'size': 10}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_size_str(self):
        self._get_os_install_device_root_device_hints(
            {'size': '10'}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_size_not_int(self):
        self.assertRaises(errors.DeviceNotFound,
                          self._get_os_install_device_root_device_hints,
                          {'size': 'not-int'}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_vendor(self):
        self._get_os_install_device_root_device_hints(
            {'vendor': 'fake-vendor'}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_name(self):
        self._get_os_install_device_root_device_hints(
            {'name': '/dev/sdb'}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_rotational(self):
        for value in (True, 'true', 'on', 'y', 'yes'):
            self._get_os_install_device_root_device_hints(
                {'rotational': value}, '/dev/sdb')

    def test_get_os_install_device_root_device_hints_by_path(self):
        self._get_os_install_device_root_device_hints(
            {'by_path': '/dev/disk/by-path/1:0:0:0'}, '/dev/sdb')

    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    @mock.patch.object(hardware, 'get_cached_node', autospec=True)
    def test_get_os_install_device_root_device_hints_no_device_found(
            self, mock_cached_node, mock_dev):
        model = 'fastable sd131 7'
        mock_cached_node.return_value = {
            'properties': {
                'root_device': {
                    'model': model,
                    'wwn': 'fake-wwn',
                    'serial': 'fake-serial',
                    'vendor': 'fake-vendor',
                    'size': 10}}}
        # Model is different here
        mock_dev.return_value = [
            hardware.BlockDevice(name='/dev/sda',
                                 model='TinyUSB Drive',
                                 size=3116853504,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 wwn='wwn0',
                                 serial='serial0'),
            hardware.BlockDevice(name='/dev/sdb',
                                 model='Another Model',
                                 size=10737418240,
                                 rotational=False,
                                 vendor='fake-vendor',
                                 wwn='fake-wwn',
                                 serial='fake-serial'),
        ]
        self.assertRaises(errors.DeviceNotFound,
                          self.hardware.get_os_install_device)
        mock_cached_node.assert_called_once_with()
        mock_dev.assert_called_once_with()

    def test__get_device_info(self):
        fileobj = mock.mock_open(read_data='fake-vendor')
        with mock.patch(
                'six.moves.builtins.open', fileobj, create=True) as mock_open:
            vendor = hardware._get_device_info(
                '/dev/sdfake', 'block', 'vendor')
            mock_open.assert_called_once_with(
                '/sys/class/block/sdfake/device/vendor', 'r')
            self.assertEqual('fake-vendor', vendor)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_cpus(self, mocked_execute):
        mocked_execute.side_effect = [
            (LSCPU_OUTPUT, ''),
            (CPUINFO_FLAGS_OUTPUT, '')
        ]

        cpus = self.hardware.get_cpus()
        self.assertEqual('Intel(R) Xeon(R) CPU E5-2609 0 @ 2.40GHz',
                         cpus.model_name)
        self.assertEqual('2400.0000', cpus.frequency)
        self.assertEqual(4, cpus.count)
        self.assertEqual('x86_64', cpus.architecture)
        self.assertEqual(['fpu', 'vme', 'de', 'pse'], cpus.flags)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_cpus2(self, mocked_execute):
        mocked_execute.side_effect = [
            (LSCPU_OUTPUT_NO_MAX_MHZ, ''),
            (CPUINFO_FLAGS_OUTPUT, '')
        ]

        cpus = self.hardware.get_cpus()
        self.assertEqual('Intel(R) Xeon(R) CPU E5-1650 v3 @ 3.50GHz',
                         cpus.model_name)
        self.assertEqual('1794.433', cpus.frequency)
        self.assertEqual(12, cpus.count)
        self.assertEqual('x86_64', cpus.architecture)
        self.assertEqual(['fpu', 'vme', 'de', 'pse'], cpus.flags)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_cpus_no_flags(self, mocked_execute):
        mocked_execute.side_effect = [
            (LSCPU_OUTPUT, ''),
            processutils.ProcessExecutionError()
        ]

        cpus = self.hardware.get_cpus()
        self.assertEqual('Intel(R) Xeon(R) CPU E5-2609 0 @ 2.40GHz',
                         cpus.model_name)
        self.assertEqual('2400.0000', cpus.frequency)
        self.assertEqual(4, cpus.count)
        self.assertEqual('x86_64', cpus.architecture)
        self.assertEqual([], cpus.flags)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_cpus_illegal_flags(self, mocked_execute):
        mocked_execute.side_effect = [
            (LSCPU_OUTPUT, ''),
            ('I am not a flag', '')
        ]

        cpus = self.hardware.get_cpus()
        self.assertEqual('Intel(R) Xeon(R) CPU E5-2609 0 @ 2.40GHz',
                         cpus.model_name)
        self.assertEqual('2400.0000', cpus.frequency)
        self.assertEqual(4, cpus.count)
        self.assertEqual('x86_64', cpus.architecture)
        self.assertEqual([], cpus.flags)

    @mock.patch('psutil.virtual_memory', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_memory_psutil_v1(self, mocked_execute, mocked_psutil):
        mocked_psutil.return_value.total = 3952 * 1024 * 1024
        mocked_execute.return_value = LSHW_JSON_OUTPUT_V1
        mem = self.hardware.get_memory()

        self.assertEqual(3952 * 1024 * 1024, mem.total)
        self.assertEqual(4096, mem.physical_mb)

    @mock.patch('psutil.virtual_memory', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_memory_psutil_v2(self, mocked_execute, mocked_psutil):
        mocked_psutil.return_value.total = 3952 * 1024 * 1024
        mocked_execute.return_value = LSHW_JSON_OUTPUT_V2
        mem = self.hardware.get_memory()

        self.assertEqual(3952 * 1024 * 1024, mem.total)
        self.assertEqual(65536, mem.physical_mb)

    @mock.patch('psutil.virtual_memory', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_memory_psutil_exception_v1(self, mocked_execute,
                                            mocked_psutil):
        mocked_execute.return_value = LSHW_JSON_OUTPUT_V1
        mocked_psutil.side_effect = AttributeError()
        mem = self.hardware.get_memory()

        self.assertIsNone(mem.total)
        self.assertEqual(4096, mem.physical_mb)

    @mock.patch('psutil.virtual_memory', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_memory_psutil_exception_v2(self, mocked_execute,
                                            mocked_psutil):
        mocked_execute.return_value = LSHW_JSON_OUTPUT_V2
        mocked_psutil.side_effect = AttributeError()
        mem = self.hardware.get_memory()

        self.assertIsNone(mem.total)
        self.assertEqual(65536, mem.physical_mb)

    @mock.patch('psutil.virtual_memory', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_memory_lshw_exception(self, mocked_execute, mocked_psutil):
        mocked_execute.side_effect = OSError()
        mocked_psutil.return_value.total = 3952 * 1024 * 1024
        mem = self.hardware.get_memory()

        self.assertEqual(3952 * 1024 * 1024, mem.total)
        self.assertIsNone(mem.physical_mb)

    @mock.patch('psutil.virtual_memory', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_memory_arm64_lshw(self, mocked_execute, mocked_psutil):
        mocked_psutil.return_value.total = 3952 * 1024 * 1024
        mocked_execute.return_value = LSHW_JSON_OUTPUT_ARM64
        mem = self.hardware.get_memory()

        self.assertEqual(3952 * 1024 * 1024, mem.total)
        self.assertEqual(3952, mem.physical_mb)

    @mock.patch('ironic_python_agent.netutils.get_hostname', autospec=True)
    def test_list_hardware_info(self, mocked_get_hostname):
        self.hardware.list_network_interfaces = mock.Mock()
        self.hardware.list_network_interfaces.return_value = [
            hardware.NetworkInterface('eth0', '00:0c:29:8c:11:b1'),
            hardware.NetworkInterface('eth1', '00:0c:29:8c:11:b2'),
        ]

        self.hardware.get_cpus = mock.Mock()
        self.hardware.get_cpus.return_value = hardware.CPU(
            'Awesome CPU x14 9001',
            9001,
            14,
            'x86_64')

        self.hardware.get_memory = mock.Mock()
        self.hardware.get_memory.return_value = hardware.Memory(1017012)

        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [
            hardware.BlockDevice('/dev/sdj', 'big', 1073741824, True),
            hardware.BlockDevice('/dev/hdaa', 'small', 65535, False),
        ]

        self.hardware.get_boot_info = mock.Mock()
        self.hardware.get_boot_info.return_value = hardware.BootInfo(
            current_boot_mode='bios', pxe_interface='boot:if')

        self.hardware.get_bmc_address = mock.Mock()
        self.hardware.get_bmc_v6address = mock.Mock()
        self.hardware.get_system_vendor_info = mock.Mock()

        mocked_get_hostname.return_value = 'mock_hostname'

        hardware_info = self.hardware.list_hardware_info()
        self.assertEqual(self.hardware.get_memory(), hardware_info['memory'])
        self.assertEqual(self.hardware.get_cpus(), hardware_info['cpu'])
        self.assertEqual(self.hardware.list_block_devices(),
                         hardware_info['disks'])
        self.assertEqual(self.hardware.list_network_interfaces(),
                         hardware_info['interfaces'])
        self.assertEqual(self.hardware.get_boot_info(),
                         hardware_info['boot'])
        self.assertEqual('mock_hostname', hardware_info['hostname'])

    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    def test_list_block_devices(self, list_mock):
        device = hardware.BlockDevice('/dev/hdaa', 'small', 65535, False)
        list_mock.return_value = [device]
        devices = self.hardware.list_block_devices()

        self.assertEqual([device], devices)

        list_mock.assert_called_once_with()

    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    def test_list_block_devices_including_partitions(self, list_mock):
        device = hardware.BlockDevice('/dev/hdaa', 'small', 65535, False)
        partition = hardware.BlockDevice('/dev/hdaa1', '', 32767, False)
        list_mock.side_effect = [[device], [partition]]
        devices = self.hardware.list_block_devices(include_partitions=True)

        self.assertEqual([device, partition], devices)

        self.assertEqual([mock.call(), mock.call(block_type='part',
                                                 ignore_raid=True)],
                         list_mock.call_args_list)

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(hardware, '_get_device_info', autospec=True)
    @mock.patch.object(pyudev.Devices, 'from_device_file', autospec=False)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_list_all_block_device(self, mocked_execute, mocked_udev,
                                   mocked_dev_vendor, mock_listdir,
                                   mock_readlink):
        by_path_map = {
            '/dev/disk/by-path/1:0:0:0': '../../dev/sda',
            '/dev/disk/by-path/1:0:0:1': '../../dev/sdb',
            '/dev/disk/by-path/1:0:0:2': '../../dev/sdc',
            # pretend that the by-path link to ../../dev/sdd is missing
        }
        mock_readlink.side_effect = lambda x, m=by_path_map: m[x]
        mock_listdir.return_value = [os.path.basename(x)
                                     for x in sorted(by_path_map)]
        mocked_execute.return_value = (BLK_DEVICE_TEMPLATE, '')
        mocked_udev.side_effect = [pyudev.DeviceNotFoundByFileError(),
                                   pyudev.DeviceNotFoundByNumberError('block',
                                                                      1234),
                                   pyudev.DeviceNotFoundByFileError(),
                                   pyudev.DeviceNotFoundByFileError()]
        mocked_dev_vendor.return_value = 'Super Vendor'
        devices = hardware.list_all_block_devices()
        expected_devices = [
            hardware.BlockDevice(name='/dev/sda',
                                 model='TinyUSB Drive',
                                 size=3116853504,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 hctl='1:0:0:0',
                                 by_path='/dev/disk/by-path/1:0:0:0'),
            hardware.BlockDevice(name='/dev/sdb',
                                 model='Fastable SD131 7',
                                 size=10737418240,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 hctl='1:0:0:0',
                                 by_path='/dev/disk/by-path/1:0:0:1'),
            hardware.BlockDevice(name='/dev/sdc',
                                 model='NWD-BLP4-1600',
                                 size=1765517033472,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 hctl='1:0:0:0',
                                 by_path='/dev/disk/by-path/1:0:0:2'),
            hardware.BlockDevice(name='/dev/sdd',
                                 model='NWD-BLP4-1600',
                                 size=1765517033472,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 hctl='1:0:0:0'),
        ]

        self.assertEqual(4, len(devices))
        for expected, device in zip(expected_devices, devices):
            # Compare all attrs of the objects
            for attr in ['name', 'model', 'size', 'rotational',
                         'wwn', 'vendor', 'serial', 'hctl']:
                self.assertEqual(getattr(expected, attr),
                                 getattr(device, attr))
        expected_calls = [mock.call('/sys/block/%s/device/scsi_device' % dev)
                          for dev in ('sda', 'sdb', 'sdc', 'sdd')]
        mock_listdir.assert_has_calls(expected_calls)

        expected_calls = [mock.call('/dev/disk/by-path/1:0:0:%d' % dev)
                          for dev in range(3)]
        mock_readlink.assert_has_calls(expected_calls)

    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(hardware, '_get_device_info', autospec=True)
    @mock.patch.object(pyudev.Devices, 'from_device_file', autospec=False)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_list_all_block_device_hctl_fail(self, mocked_execute, mocked_udev,
                                             mocked_dev_vendor,
                                             mocked_listdir):
        mocked_listdir.side_effect = (OSError, OSError, IndexError)
        mocked_execute.return_value = (BLK_DEVICE_TEMPLATE_SMALL, '')
        mocked_dev_vendor.return_value = 'Super Vendor'
        devices = hardware.list_all_block_devices()
        self.assertEqual(2, len(devices))
        expected_calls = [
            mock.call('/dev/disk/by-path'),
            mock.call('/sys/block/sda/device/scsi_device'),
            mock.call('/sys/block/sdb/device/scsi_device')
        ]
        self.assertEqual(expected_calls, mocked_listdir.call_args_list)

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os, 'listdir', autospec=True)
    @mock.patch.object(hardware, '_get_device_info', autospec=True)
    @mock.patch.object(pyudev.Devices, 'from_device_file', autospec=False)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_list_all_block_device_with_udev(self, mocked_execute, mocked_udev,
                                             mocked_dev_vendor, mocked_listdir,
                                             mocked_readlink):
        mocked_readlink.return_value = '../../sda'
        mocked_listdir.return_value = ['1:0:0:0']
        mocked_execute.return_value = (BLK_DEVICE_TEMPLATE, '')
        mocked_udev.side_effect = iter([
            {'ID_WWN': 'wwn%d' % i, 'ID_SERIAL_SHORT': 'serial%d' % i,
             'ID_WWN_WITH_EXTENSION': 'wwn-ext%d' % i,
             'ID_WWN_VENDOR_EXTENSION': 'wwn-vendor-ext%d' % i}
            for i in range(4)
        ])
        mocked_dev_vendor.return_value = 'Super Vendor'
        devices = hardware.list_all_block_devices()
        expected_devices = [
            hardware.BlockDevice(name='/dev/sda',
                                 model='TinyUSB Drive',
                                 size=3116853504,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 wwn='wwn0',
                                 wwn_with_extension='wwn-ext0',
                                 wwn_vendor_extension='wwn-vendor-ext0',
                                 serial='serial0',
                                 hctl='1:0:0:0'),
            hardware.BlockDevice(name='/dev/sdb',
                                 model='Fastable SD131 7',
                                 size=10737418240,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 wwn='wwn1',
                                 wwn_with_extension='wwn-ext1',
                                 wwn_vendor_extension='wwn-vendor-ext1',
                                 serial='serial1',
                                 hctl='1:0:0:0'),
            hardware.BlockDevice(name='/dev/sdc',
                                 model='NWD-BLP4-1600',
                                 size=1765517033472,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 wwn='wwn2',
                                 wwn_with_extension='wwn-ext2',
                                 wwn_vendor_extension='wwn-vendor-ext2',
                                 serial='serial2',
                                 hctl='1:0:0:0'),
            hardware.BlockDevice(name='/dev/sdd',
                                 model='NWD-BLP4-1600',
                                 size=1765517033472,
                                 rotational=False,
                                 vendor='Super Vendor',
                                 wwn='wwn3',
                                 wwn_with_extension='wwn-ext3',
                                 wwn_vendor_extension='wwn-vendor-ext3',
                                 serial='serial3',
                                 hctl='1:0:0:0')
        ]

        self.assertEqual(4, len(expected_devices))
        for expected, device in zip(expected_devices, devices):
            # Compare all attrs of the objects
            for attr in ['name', 'model', 'size', 'rotational',
                         'wwn', 'vendor', 'serial', 'wwn_with_extension',
                         'wwn_vendor_extension', 'hctl']:
                self.assertEqual(getattr(expected, attr),
                                 getattr(device, attr))
        expected_calls = [mock.call('/sys/block/%s/device/scsi_device' % dev)
                          for dev in ('sda', 'sdb', 'sdc', 'sdd')]
        mocked_listdir.assert_has_calls(expected_calls)

    @mock.patch.object(hardware, 'dispatch_to_managers', autospec=True)
    def test_erase_devices_no_parallel_by_default(self, mocked_dispatch):
        mocked_dispatch.return_value = 'erased device'

        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [
            hardware.BlockDevice('/dev/sdj', 'big', 1073741824, True),
            hardware.BlockDevice('/dev/hdaa', 'small', 65535, False),
        ]

        expected = {'/dev/hdaa': 'erased device', '/dev/sdj': 'erased device'}

        result = self.hardware.erase_devices({}, [])

        self.assertEqual(expected, result)

    @mock.patch('multiprocessing.pool.ThreadPool.apply_async', autospec=True)
    @mock.patch.object(hardware, 'dispatch_to_managers', autospec=True)
    def test_erase_devices_concurrency(self, mocked_dispatch, mocked_async):
        internal_info = self.node['driver_internal_info']
        internal_info['disk_erasure_concurrency'] = 10
        mocked_dispatch.return_value = 'erased device'

        if six.PY3:
            apply_result = multiprocessing.pool.ApplyResult({}, None, None)
        else:
            apply_result = multiprocessing.pool.ApplyResult({}, None)
        apply_result._success = True
        apply_result._ready = True
        apply_result.get = lambda: 'erased device'
        mocked_async.return_value = apply_result

        blkdev1 = hardware.BlockDevice('/dev/sdj', 'big', 1073741824, True)
        blkdev2 = hardware.BlockDevice('/dev/hdaa', 'small', 65535, False)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [blkdev1, blkdev2]

        expected = {'/dev/hdaa': 'erased device', '/dev/sdj': 'erased device'}

        result = self.hardware.erase_devices(self.node, [])

        calls = [mock.call(mock.ANY, mocked_dispatch, ('erase_block_device',),
                           {'node': self.node, 'block_device': dev})
                 for dev in (blkdev1, blkdev2)]
        mocked_async.assert_has_calls(calls)
        self.assertEqual(expected, result)

    @mock.patch.object(hardware, 'ThreadPool', autospec=True)
    def test_erase_devices_concurrency_pool_size(self, mocked_pool):
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [
            hardware.BlockDevice('/dev/sdj', 'big', 1073741824, True),
            hardware.BlockDevice('/dev/hdaa', 'small', 65535, False),
        ]

        # Test pool size 10 with 2 disks
        internal_info = self.node['driver_internal_info']
        internal_info['disk_erasure_concurrency'] = 10

        self.hardware.erase_devices(self.node, [])
        mocked_pool.assert_called_with(2)

        # Test default pool size with 2 disks
        internal_info = self.node['driver_internal_info']
        del internal_info['disk_erasure_concurrency']

        self.hardware.erase_devices(self.node, [])
        mocked_pool.assert_called_with(1)

    @mock.patch.object(hardware, 'dispatch_to_managers', autospec=True)
    def test_erase_devices_without_disk(self, mocked_dispatch):
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = []

        expected = {}
        result = self.hardware.erase_devices({}, [])
        self.assertEqual(expected, result)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_success(self, mocked_execute,
                                            mocked_raid_member):
        mocked_execute.side_effect = [
            (create_hdparm_info(
                supported=True, enabled=False, frozen=False,
                enhanced_erase=False), ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            ('', ''),
            ('', ''),
            (create_hdparm_info(
                supported=True, enabled=False, frozen=False,
                enhanced_erase=False), ''),
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('hdparm', '--user-master', 'u', '--security-set-pass',
                      'NULL', '/dev/sda'),
            mock.call('hdparm', '--user-master', 'u', '--security-erase',
                      'NULL', '/dev/sda'),
            mock.call('hdparm', '-I', '/dev/sda'),
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_success_no_smartctl(self, mocked_execute,
                                                        mocked_raid_member):
        mocked_execute.side_effect = [
            (create_hdparm_info(
                supported=True, enabled=False, frozen=False,
                enhanced_erase=False), ''),
            OSError('boom'),
            ('', ''),
            ('', ''),
            (create_hdparm_info(
                supported=True, enabled=False, frozen=False,
                enhanced_erase=False), ''),
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('hdparm', '--user-master', 'u', '--security-set-pass',
                      'NULL', '/dev/sda'),
            mock.call('hdparm', '--user-master', 'u', '--security-erase',
                      'NULL', '/dev/sda'),
            mock.call('hdparm', '-I', '/dev/sda'),
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_nosecurity_shred(self, mocked_execute,
                                                 mocked_raid_member):
        hdparm_output = HDPARM_INFO_TEMPLATE.split('\nSecurity:')[0]

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_UNAVAILABLE_OUTPUT, ''),
            (SHRED_OUTPUT_1_ITERATION_ZERO_TRUE, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('shred', '--force', '--zero', '--verbose',
                      '--iterations', '1', '/dev/sda')
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_notsupported_shred(self, mocked_execute,
                                                   mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=False, enabled=False, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_UNAVAILABLE_OUTPUT, ''),
            (SHRED_OUTPUT_1_ITERATION_ZERO_TRUE, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('shred', '--force', '--zero', '--verbose',
                      '--iterations', '1', '/dev/sda')
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_smartctl_unsupported_shred(self,
                                                           mocked_execute,
                                                           mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_UNAVAILABLE_OUTPUT, ''),
            (SHRED_OUTPUT_1_ITERATION_ZERO_TRUE, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('shred', '--force', '--zero', '--verbose',
                      '--iterations', '1', '/dev/sda')
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_smartctl_fails_security_fallback_to_shred(
            self, mocked_execute, mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            processutils.ProcessExecutionError(),
            (SHRED_OUTPUT_1_ITERATION_ZERO_TRUE, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('shred', '--force', '--zero', '--verbose',
                      '--iterations', '1', '/dev/sda')
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_shred_uses_internal_info(self, mocked_execute,
                                                         mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=False, enabled=False, frozen=False, enhanced_erase=False)

        info = self.node['driver_internal_info']
        info['agent_erase_devices_iterations'] = 2
        info['agent_erase_devices_zeroize'] = False

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            (SHRED_OUTPUT_2_ITERATIONS_ZERO_FALSE, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('shred', '--force', '--verbose',
                      '--iterations', '2', '/dev/sda')
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_shred_0_pass_no_zeroize(self, mocked_execute,
                                                        mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=False, enabled=False, frozen=False, enhanced_erase=False)

        info = self.node['driver_internal_info']
        info['agent_erase_devices_iterations'] = 0
        info['agent_erase_devices_zeroize'] = False

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_UNAVAILABLE_OUTPUT, ''),
            (SHRED_OUTPUT_0_ITERATIONS_ZERO_FALSE, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        mocked_execute.assert_has_calls([
            mock.call('hdparm', '-I', '/dev/sda'),
            mock.call('smartctl', '-d', 'ata', '/dev/sda', '-g', 'security',
                      check_exit_code=[0, 127]),
            mock.call('shred', '--force', '--verbose',
                      '--iterations', '0', '/dev/sda')
        ])

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_virtual_media_device', autospec=True)
    def test_erase_block_device_virtual_media(self, vm_mock):
        vm_mock.return_value = True
        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.hardware.erase_block_device(self.node, block_device)
        vm_mock.assert_called_once_with(self.hardware, block_device)

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os.path, 'exists', autospec=True)
    def test__is_virtual_media_device_exists(self, mocked_exists,
                                             mocked_link):
        mocked_exists.return_value = True
        mocked_link.return_value = '../../sda'
        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        res = self.hardware._is_virtual_media_device(block_device)
        self.assertTrue(res)
        mocked_exists.assert_called_once_with('/dev/disk/by-label/ir-vfd-dev')
        mocked_link.assert_called_once_with('/dev/disk/by-label/ir-vfd-dev')

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os.path, 'exists', autospec=True)
    def test__is_virtual_media_device_exists_no_match(self, mocked_exists,
                                                      mocked_link):
        mocked_exists.return_value = True
        mocked_link.return_value = '../../sdb'
        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        res = self.hardware._is_virtual_media_device(block_device)
        self.assertFalse(res)
        mocked_exists.assert_called_once_with('/dev/disk/by-label/ir-vfd-dev')
        mocked_link.assert_called_once_with('/dev/disk/by-label/ir-vfd-dev')

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(os.path, 'exists', autospec=True)
    def test__is_virtual_media_device_path_doesnt_exist(self, mocked_exists,
                                                        mocked_link):
        mocked_exists.return_value = False
        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        res = self.hardware._is_virtual_media_device(block_device)
        self.assertFalse(res)
        mocked_exists.assert_called_once_with('/dev/disk/by-label/ir-vfd-dev')
        self.assertFalse(mocked_link.called)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_shred_fail_oserror(self, mocked_execute):
        mocked_execute.side_effect = OSError
        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        res = self.hardware._shred_block_device(self.node, block_device)
        self.assertFalse(res)
        mocked_execute.assert_called_once_with(
            'shred', '--force', '--zero', '--verbose', '--iterations', '1',
            '/dev/sda')

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_shred_fail_processerror(self, mocked_execute):
        mocked_execute.side_effect = processutils.ProcessExecutionError
        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        res = self.hardware._shred_block_device(self.node, block_device)
        self.assertFalse(res)
        mocked_execute.assert_called_once_with(
            'shred', '--force', '--zero', '--verbose', '--iterations', '1',
            '/dev/sda')

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_security_unlock_fallback_pass(
            self, mocked_execute, mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=True, enabled=True, locked=True
        )
        hdparm_output_unlocked = create_hdparm_info(
            supported=True, enabled=True, frozen=False, enhanced_erase=False)
        hdparm_output_not_enabled = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)
        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            processutils.ProcessExecutionError(),  # NULL fails to unlock
            (hdparm_output, ''),  # recheck security lines
            None,  # security unlock with ""
            (hdparm_output_unlocked, ''),
            '',
            (hdparm_output_not_enabled, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.hardware.erase_block_device(self.node, block_device)

        mocked_execute.assert_any_call('hdparm', '--user-master', 'u',
                                       '--security-unlock', '', '/dev/sda')

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager, '_shred_block_device',
                       autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_security_enabled(
            self, mocked_execute, mock_shred, mocked_raid_member):
        # Tests that an exception is thrown if all of the recovery passwords
        # fail to unlock the device without throwing exception
        hdparm_output = create_hdparm_info(
            supported=True, enabled=True, locked=True)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            None,
            (hdparm_output, ''),
            None,
            (hdparm_output, ''),
            None,
            (hdparm_output, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.assertRaises(
            errors.IncompatibleHardwareMethodError,
            self.hardware.erase_block_device,
            self.node,
            block_device)
        mocked_execute.assert_any_call('hdparm', '--user-master', 'u',
                                       '--security-unlock', '', '/dev/sda')
        mocked_execute.assert_any_call('hdparm', '--user-master', 'u',
                                       '--security-unlock', 'NULL', '/dev/sda')
        self.assertFalse(mock_shred.called)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager, '_shred_block_device',
                       autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_security_enabled_unlock_attempt(
            self, mocked_execute, mock_shred, mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=True, enabled=True, locked=True)
        hdparm_output_not_enabled = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            '',
            (hdparm_output_not_enabled, ''),
            '',
            '',
            (hdparm_output_not_enabled, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.hardware.erase_block_device(self.node, block_device)
        self.assertFalse(mock_shred.called)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__ata_erase_security_enabled_unlock_exception(
            self, mocked_execute):
        # test that an exception is thrown when security unlock fails with
        # ProcessExecutionError
        hdparm_output = create_hdparm_info(
            supported=True, enabled=True, locked=True)
        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            processutils.ProcessExecutionError(),
            (hdparm_output, ''),
            processutils.ProcessExecutionError(),
            (hdparm_output, ''),
        ]

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.assertRaises(errors.BlockDeviceEraseError,
                          self.hardware._ata_erase,
                          block_device)
        mocked_execute.assert_any_call('hdparm', '--user-master', 'u',
                                       '--security-unlock', '', '/dev/sda')
        mocked_execute.assert_any_call('hdparm', '--user-master', 'u',
                                       '--security-unlock', 'NULL', '/dev/sda')

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__ata_erase_security_enabled_set_password_exception(
            self, mocked_execute):
        hdparm_output = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            processutils.ProcessExecutionError()
        ]

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.assertRaises(errors.BlockDeviceEraseError,
                          self.hardware._ata_erase,
                          block_device)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__ata_erase_security_erase_exec_exception(
            self, mocked_execute):
        # Exception on security erase
        hdparm_output = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)
        hdparm_unlocked_output = create_hdparm_info(
            supported=True, locked=True, frozen=False, enhanced_erase=False)
        mocked_execute.side_effect = [
            (hdparm_output, '', '-1'),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            '',  # security-set-pass
            processutils.ProcessExecutionError(),  # security-erase
            (hdparm_unlocked_output, '', '-1'),
            '',  # attempt security unlock
            (hdparm_output, '', '-1')
        ]

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.assertRaises(errors.BlockDeviceEraseError,
                          self.hardware._ata_erase,
                          block_device)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager, '_shred_block_device',
                       autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_frozen(self, mocked_execute, mock_shred,
                                           mocked_raid_member):
        hdparm_output = create_hdparm_info(
            supported=True, enabled=False, frozen=True, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output, ''),
            (SMARTCTL_NORMAL_OUTPUT, '')
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)
        self.assertRaises(
            errors.IncompatibleHardwareMethodError,
            self.hardware.erase_block_device,
            self.node,
            block_device)
        self.assertFalse(mock_shred.called)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager, '_shred_block_device',
                       autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_failed(self, mocked_execute, mock_shred,
                                           mocked_raid_member):
        hdparm_output_before = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)

        # If security mode remains enabled after the erase, it is indicative
        # of a failed erase.
        hdparm_output_after = create_hdparm_info(
            supported=True, enabled=True, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output_before, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            ('', ''),
            ('', ''),
            (hdparm_output_after, ''),
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.assertRaises(
            errors.IncompatibleHardwareMethodError,
            self.hardware.erase_block_device,
            self.node,
            block_device)
        self.assertFalse(mock_shred.called)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager, '_shred_block_device',
                       autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_failed_continued(
            self, mocked_execute, mock_shred, mocked_raid_member):

        info = self.node['driver_internal_info']
        info['agent_continue_if_ata_erase_failed'] = True

        hdparm_output_before = create_hdparm_info(
            supported=True, enabled=False, frozen=False, enhanced_erase=False)

        # If security mode remains enabled after the erase, it is indicative
        # of a failed erase.
        hdparm_output_after = create_hdparm_info(
            supported=True, enabled=True, frozen=False, enhanced_erase=False)

        mocked_execute.side_effect = [
            (hdparm_output_before, ''),
            (SMARTCTL_NORMAL_OUTPUT, ''),
            ('', ''),
            ('', ''),
            (hdparm_output_after, ''),
        ]
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.hardware.erase_block_device(self.node, block_device)
        self.assertTrue(mock_shred.called)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager, '_shred_block_device',
                       autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_erase_block_device_ata_erase_disabled(
            self, mocked_execute, mock_shred, mocked_raid_member):

        info = self.node['driver_internal_info']
        info['agent_enable_ata_secure_erase'] = False
        mocked_raid_member.return_value = False

        block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                            True)

        self.hardware.erase_block_device(self.node, block_device)
        self.assertTrue(mock_shred.called)
        self.assertFalse(mocked_execute.called)

    def test_normal_vs_enhanced_security_erase(self):
        @mock.patch.object(hardware.GenericHardwareManager,
                           '_is_linux_raid_member', autospec=True)
        @mock.patch.object(utils, 'execute', autospec=True)
        def test_security_erase_option(test_case,
                                       enhanced_erase,
                                       expected_option,
                                       mocked_execute,
                                       mocked_raid_member):
            mocked_execute.side_effect = [
                (create_hdparm_info(
                    supported=True, enabled=False, frozen=False,
                    enhanced_erase=enhanced_erase), ''),
                (SMARTCTL_NORMAL_OUTPUT, ''),
                ('', ''),
                ('', ''),
                (create_hdparm_info(
                    supported=True, enabled=False, frozen=False,
                    enhanced_erase=enhanced_erase), ''),
            ]
            mocked_raid_member.return_value = False

            block_device = hardware.BlockDevice('/dev/sda', 'big', 1073741824,
                                                True)
            test_case.hardware.erase_block_device(self.node, block_device)
            mocked_execute.assert_any_call('hdparm', '--user-master', 'u',
                                           expected_option,
                                           'NULL', '/dev/sda')

        test_security_erase_option(
            self, True, '--security-erase-enhanced')
        test_security_erase_option(
            self, False, '--security-erase')

    @mock.patch.object(utils, 'execute', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_virtual_media_device', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager,
                       'list_block_devices', autospec=True)
    @mock.patch.object(disk_utils, 'destroy_disk_metadata', autospec=True)
    def test_erase_devices_metadata(
            self, mock_metadata, mock_list_devs, mock__is_vmedia,
            mock_execute):
        block_devices = [
            hardware.BlockDevice('/dev/sr0', 'vmedia', 12345, True),
            hardware.BlockDevice('/dev/sdb2', 'raid-member', 32767, False),
            hardware.BlockDevice('/dev/sda', 'small', 65535, False),
            hardware.BlockDevice('/dev/sda1', '', 32767, False),
            hardware.BlockDevice('/dev/sda2', 'raid-member', 32767, False),
            hardware.BlockDevice('/dev/md0', 'raid-device', 32767, False)
        ]
        # NOTE(coreywright): Don't return the list, but a copy of it, because
        # we depend on its elements' order when referencing it later during
        # verification, but the method under test sorts the list changing it.
        mock_list_devs.return_value = list(block_devices)
        mock__is_vmedia.side_effect = lambda _, dev: dev.name == '/dev/sr0'
        mock_execute.side_effect = [
            ('sdb2 linux_raid_member host:1 f9978968', ''),
            ('sda2 linux_raid_member host:1 f9978969', ''),
            ('sda1', ''), ('sda', ''), ('md0', '')]

        self.hardware.erase_devices_metadata(self.node, [])

        self.assertEqual([mock.call('/dev/sda1', self.node['uuid']),
                          mock.call('/dev/sda', self.node['uuid']),
                          mock.call('/dev/md0', self.node['uuid'])],
                         mock_metadata.call_args_list)
        mock_list_devs.assert_called_once_with(self.hardware,
                                               include_partitions=True)
        self.assertEqual([mock.call(self.hardware, block_devices[0]),
                          mock.call(self.hardware, block_devices[1]),
                          mock.call(self.hardware, block_devices[4]),
                          mock.call(self.hardware, block_devices[3]),
                          mock.call(self.hardware, block_devices[2]),
                          mock.call(self.hardware, block_devices[5])],
                         mock__is_vmedia.call_args_list)

    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_linux_raid_member', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager,
                       '_is_virtual_media_device', autospec=True)
    @mock.patch.object(hardware.GenericHardwareManager,
                       'list_block_devices', autospec=True)
    @mock.patch.object(disk_utils, 'destroy_disk_metadata', autospec=True)
    def test_erase_devices_metadata_error(
            self, mock_metadata, mock_list_devs, mock__is_vmedia,
            mock__is_raid_member):
        block_devices = [
            hardware.BlockDevice('/dev/sda', 'small', 65535, False),
            hardware.BlockDevice('/dev/sdb', 'big', 10737418240, True),
        ]
        mock__is_vmedia.return_value = False
        mock__is_raid_member.return_value = False
        # NOTE(coreywright): Don't return the list, but a copy of it, because
        # we depend on its elements' order when referencing it later during
        # verification, but the method under test sorts the list changing it.
        mock_list_devs.return_value = list(block_devices)
        # Simulate first call to destroy_disk_metadata() failing, which is for
        # /dev/sdb due to erase_devices_metadata() reverse sorting block
        # devices by name, and second call succeeding, which is for /dev/sda
        error_output = 'Booo00000ooommmmm'
        error_regex = '(?s)/dev/sdb.*' + error_output
        mock_metadata.side_effect = (
            processutils.ProcessExecutionError(error_output),
            None,
        )

        self.assertRaisesRegex(errors.BlockDeviceEraseError, error_regex,
                               self.hardware.erase_devices_metadata,
                               self.node, [])
        # Assert all devices are erased independent if one of them
        # failed previously
        self.assertEqual([mock.call('/dev/sdb', self.node['uuid']),
                          mock.call('/dev/sda', self.node['uuid'])],
                         mock_metadata.call_args_list)
        mock_list_devs.assert_called_once_with(self.hardware,
                                               include_partitions=True)
        self.assertEqual([mock.call(self.hardware, block_devices[1]),
                          mock.call(self.hardware, block_devices[0])],
                         mock__is_vmedia.call_args_list)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__is_linux_raid_member(self, mocked_execute):
        raid_member = hardware.BlockDevice('/dev/sda1', 'small', 65535, False)
        mocked_execute.return_value = ('linux_raid_member host.domain:0 '
                                       '85fa41e4-e0ae'), ''
        self.assertTrue(self.hardware._is_linux_raid_member(raid_member))

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__is_linux_raid_member_false(self, mocked_execute):
        raid_member = hardware.BlockDevice('/dev/md0', 'small', 65535, False)
        mocked_execute.return_value = 'md0', ''
        self.assertFalse(self.hardware._is_linux_raid_member(raid_member))

    def test__is_read_only_device(self):
        fileobj = mock.mock_open(read_data='1\n')
        device = hardware.BlockDevice('/dev/sdfake', 'fake', 1024, False)
        with mock.patch(
                'six.moves.builtins.open', fileobj, create=True) as mock_open:
            self.assertTrue(self.hardware._is_read_only_device(device))
            mock_open.assert_called_once_with(
                '/sys/block/sdfake/ro', 'r')

    def test__is_read_only_device_false(self):
        fileobj = mock.mock_open(read_data='0\n')
        device = hardware.BlockDevice('/dev/sdfake', 'fake', 1024, False)
        with mock.patch(
                'six.moves.builtins.open', fileobj, create=True) as mock_open:
            self.assertFalse(self.hardware._is_read_only_device(device))
            mock_open.assert_called_once_with(
                '/sys/block/sdfake/ro', 'r')

    def test__is_read_only_device_error(self):
        device = hardware.BlockDevice('/dev/sdfake', 'fake', 1024, False)
        with mock.patch(
                'six.moves.builtins.open', side_effect=IOError,
                autospec=True) as mock_open:
            self.assertFalse(self.hardware._is_read_only_device(device))
            mock_open.assert_called_once_with(
                '/sys/block/sdfake/ro', 'r')

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address(self, mocked_execute):
        mocked_execute.return_value = '192.1.2.3\n', ''
        self.assertEqual('192.1.2.3', self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address_virt(self, mocked_execute):
        mocked_execute.side_effect = processutils.ProcessExecutionError()
        self.assertIsNone(self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address_zeroed(self, mocked_execute):
        mocked_execute.return_value = '0.0.0.0\n', ''
        self.assertEqual('0.0.0.0', self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address_invalid(self, mocked_execute):
        # In case of invalid lan channel, stdout is empty and the error
        # on stderr is "Invalid channel"
        mocked_execute.return_value = '\n', 'Invalid channel: 55'
        self.assertEqual('0.0.0.0', self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address_random_error(self, mocked_execute):
        mocked_execute.return_value = '192.1.2.3\n', 'Random error message'
        self.assertEqual('192.1.2.3', self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address_iterate_channels(self, mocked_execute):
        # For channel 1 we simulate unconfigured IP
        # and for any other we return a correct IP address
        def side_effect(*args, **kwargs):
            if args[0].startswith("ipmitool lan print 1"):
                return '', 'Invalid channel 1\n'
            elif args[0].startswith("ipmitool lan print 2"):
                return '0.0.0.0\n', ''
            elif args[0].startswith("ipmitool lan print 3"):
                return 'meow', ''
            else:
                return '192.1.2.3\n', ''
        mocked_execute.side_effect = side_effect
        self.assertEqual('192.1.2.3', self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_address_not_available(self, mocked_execute):
        mocked_execute.return_value = '', ''
        self.assertEqual('0.0.0.0', self.hardware.get_bmc_address())

    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_not_enabled(self, mocked_execute, mte):
        mocked_execute.side_effect = [('ipv4\n', '')] * 11
        self.assertEqual('::/0', self.hardware.get_bmc_v6address())

    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_dynamic_address(self, mocked_execute, mte):
        mocked_execute.side_effect = [
            ('ipv6\n', ''),
            (IPMITOOL_LAN6_PRINT_DYNAMIC_ADDR, '')
        ]
        self.assertEqual('2001:1234:1234:1234:1234:1234:1234:1234',
                         self.hardware.get_bmc_v6address())

    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_static_address_both(self, mocked_execute, mte):
        dynamic_disabled = \
            IPMITOOL_LAN6_PRINT_DYNAMIC_ADDR.replace('active', 'disabled')
        mocked_execute.side_effect = [
            ('both\n', ''),
            (dynamic_disabled, ''),
            (IPMITOOL_LAN6_PRINT_STATIC_ADDR, '')
        ]
        self.assertEqual('2001:5678:5678:5678:5678:5678:5678:5678',
                         self.hardware.get_bmc_v6address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_virt(self, mocked_execute):
        mocked_execute.side_effect = processutils.ProcessExecutionError()
        self.assertIsNone(self.hardware.get_bmc_v6address())

    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_invalid_enables(self, mocked_execute, mte):
        def side_effect(*args, **kwargs):
            if args[0].startswith('ipmitool lan6 print'):
                return '', 'Failed to get IPv6/IPv4 Addressing Enables'

        mocked_execute.side_effect = side_effect
        self.assertEqual('::/0', self.hardware.get_bmc_v6address())

    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_invalid_get_address(self, mocked_execute, mte):
        def side_effect(*args, **kwargs):
            if args[0].startswith('ipmitool lan6 print'):
                if args[0].endswith('dynamic_addr') \
                        or args[0].endswith('static_addr'):
                    raise processutils.ProcessExecutionError()
                return 'ipv6', ''

        mocked_execute.side_effect = side_effect
        self.assertEqual('::/0', self.hardware.get_bmc_v6address())

    @mock.patch.object(hardware, 'LOG', autospec=True)
    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_ipmitool_invalid_stdout_format(
            self, mocked_execute, mte, mocked_log):
        def side_effect(*args, **kwargs):
            if args[0].startswith('ipmitool lan6 print'):
                if args[0].endswith('dynamic_addr') \
                        or args[0].endswith('static_addr'):
                    return 'Invalid\n\tyaml', ''
                return 'ipv6', ''

        mocked_execute.side_effect = side_effect
        self.assertEqual('::/0', self.hardware.get_bmc_v6address())
        one_call = mock.call('Cannot process output of "%(cmd)s" '
                             'command: %(e)s', mock.ANY)
        mocked_log.warning.assert_has_calls([one_call] * 14)

    @mock.patch.object(utils, 'try_execute', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_bmc_v6address_channel_7(self, mocked_execute, mte):
        def side_effect(*args, **kwargs):
            if not args[0].startswith('ipmitool lan6 print 7'):
                # ipv6 is not enabled for channels 1-6
                if 'enables |' in args[0]:
                    return '', ''
            else:
                if 'enables |' in args[0]:
                    return 'ipv6', ''
                if args[0].endswith('dynamic_addr'):
                    raise processutils.ProcessExecutionError()
                elif args[0].endswith('static_addr'):
                    return IPMITOOL_LAN6_PRINT_STATIC_ADDR, ''

        mocked_execute.side_effect = side_effect
        self.assertEqual('2001:5678:5678:5678:5678:5678:5678:5678',
                         self.hardware.get_bmc_v6address())

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_validate_configuration_no_configuration(self, mocked_execute):
        self.assertRaises(errors.SoftwareRAIDError,
                          self.hardware.validate_configuration,
                          self.node, [])

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration(self, mocked_execute):
        node = self.node
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "10",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/sda', 'sda', 107374182400, True)
        device2 = hardware.BlockDevice('/dev/sdb', 'sdb', 107374182400, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [device1, device2]

        mocked_execute.side_effect = [
            None,  # mklabel sda
            ('42', None),  # sgdisk -F sda
            None,  # mklabel sda
            ('42', None),  # sgdisk -F sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None  # mdadms
        ]

        result = self.hardware.create_configuration(node, [])

        mocked_execute.assert_has_calls([
            mock.call('parted', '/dev/sda', '-s', '--', 'mklabel',
                      'msdos'),
            mock.call('sgdisk', '-F', '/dev/sda'),
            mock.call('parted', '/dev/sdb', '-s', '--', 'mklabel',
                      'msdos'),
            mock.call('sgdisk', '-F', '/dev/sdb'),
            mock.call('parted', '/dev/sda', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '10GiB'),
            mock.call('partx', '-u', '/dev/sda', check_exit_code=False),
            mock.call('parted', '/dev/sdb', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '10GiB'),
            mock.call('partx', '-u', '/dev/sdb', check_exit_code=False),
            mock.call('parted', '/dev/sda', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '10GiB', '-1'),
            mock.call('partx', '-u', '/dev/sda', check_exit_code=False),
            mock.call('parted', '/dev/sdb', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '10GiB', '-1'),
            mock.call('partx', '-u', '/dev/sdb', check_exit_code=False),
            mock.call('mdadm', '--create', '/dev/md0', '--force', '--run',
                      '--metadata=1', '--level', '1', '--raid-devices', 2,
                      '/dev/sda1', '/dev/sdb1'),
            mock.call('mdadm', '--create', '/dev/md1', '--force', '--run',
                      '--metadata=1', '--level', '0', '--raid-devices', 2,
                      '/dev/sda2', '/dev/sdb2')])
        self.assertEqual(raid_config, result)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_no_max(self, mocked_execute):
        node = self.node
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "10",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "20",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }

        node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/sda', 'sda', 107374182400, True)
        device2 = hardware.BlockDevice('/dev/sdb', 'sdb', 107374182400, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [device1, device2]

        mocked_execute.side_effect = [
            None,  # mklabel sda
            ('42', None),  # sgdisk -F sda
            None,  # mklabel sda
            ('42', None),  # sgdisk -F sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None  # mdadms
        ]

        result = self.hardware.create_configuration(node, [])

        mocked_execute.assert_has_calls([
            mock.call('parted', '/dev/sda', '-s', '--', 'mklabel', 'msdos'),
            mock.call('sgdisk', '-F', '/dev/sda'),
            mock.call('parted', '/dev/sdb', '-s', '--', 'mklabel', 'msdos'),
            mock.call('sgdisk', '-F', '/dev/sdb'),
            mock.call('parted', '/dev/sda', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '10GiB'),
            mock.call('partx', '-u', '/dev/sda', check_exit_code=False),
            mock.call('parted', '/dev/sdb', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '10GiB'),
            mock.call('partx', '-u', '/dev/sdb', check_exit_code=False),
            mock.call('parted', '/dev/sda', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '10GiB', '30GiB'),
            mock.call('partx', '-u', '/dev/sda', check_exit_code=False),
            mock.call('parted', '/dev/sdb', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '10GiB', '30GiB'),
            mock.call('partx', '-u', '/dev/sdb', check_exit_code=False),
            mock.call('mdadm', '--create', '/dev/md0', '--force', '--run',
                      '--metadata=1', '--level', '1', '--raid-devices', 2,
                      '/dev/sda1', '/dev/sdb1'),
            mock.call('mdadm', '--create', '/dev/md1', '--force', '--run',
                      '--metadata=1', '--level', '0', '--raid-devices', 2,
                      '/dev/sda2', '/dev/sdb2')])
        self.assertEqual(raid_config, result)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_max_is_first_logical(self, mocked_execute):
        node = self.node
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "MAX",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "20",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }

        node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/sda', 'sda', 107374182400, True)
        device2 = hardware.BlockDevice('/dev/sdb', 'sdb', 107374182400, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [device1, device2]

        mocked_execute.side_effect = [
            None,  # mklabel sda
            ('42', None),  # sgdisk -F sda
            None,  # mklabel sda
            ('42', None),  # sgdisk -F sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None  # mdadms
        ]

        result = self.hardware.create_configuration(node, [])

        mocked_execute.assert_has_calls([
            mock.call('parted', '/dev/sda', '-s', '--', 'mklabel', 'msdos'),
            mock.call('sgdisk', '-F', '/dev/sda'),
            mock.call('parted', '/dev/sdb', '-s', '--', 'mklabel', 'msdos'),
            mock.call('sgdisk', '-F', '/dev/sdb'),
            mock.call('parted', '/dev/sda', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '20GiB'),
            mock.call('partx', '-u', '/dev/sda', check_exit_code=False),
            mock.call('parted', '/dev/sdb', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '20GiB'),
            mock.call('partx', '-u', '/dev/sdb', check_exit_code=False),
            mock.call('parted', '/dev/sda', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '20GiB', '-1'),
            mock.call('partx', '-u', '/dev/sda', check_exit_code=False),
            mock.call('parted', '/dev/sdb', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '20GiB', '-1'),
            mock.call('partx', '-u', '/dev/sdb', check_exit_code=False),
            mock.call('mdadm', '--create', '/dev/md0', '--force', '--run',
                      '--metadata=1', '--level', '0', '--raid-devices', 2,
                      '/dev/sda1', '/dev/sdb1'),
            mock.call('mdadm', '--create', '/dev/md1', '--force', '--run',
                      '--metadata=1', '--level', '1', '--raid-devices', 2,
                      '/dev/sda2', '/dev/sdb2')])
        self.assertEqual(raid_config, result)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_invalid_raid_config(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "MAX",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.node['target_raid_config'] = raid_config
        self.assertRaises(errors.SoftwareRAIDError,
                          self.hardware.create_configuration,
                          self.node, [])

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_partitions_detected(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "100",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/sda', 'sda', 107374182400, True)
        device2 = hardware.BlockDevice('/dev/sdb', 'sdb', 107374182400, True)
        partition1 = hardware.BlockDevice('/dev/sdb1', 'sdb1', 268435456, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.side_effect = [
            [device1, device2],
            [device1, device2, partition1]]
        self.assertRaises(errors.SoftwareRAIDError,
                          self.hardware.create_configuration,
                          self.node, [])

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_device_handling_failures(self,
                                                           mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "100",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/sda', 'sda', 107374182400, True)
        device2 = hardware.BlockDevice('/dev/sdb', 'sdb', 107374182400, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.side_effect = [
            [device1, device2],
            [device1, device2],
            [device1, device2],
            [device1, device2],
            [device1, device2],
            [device1, device2]]

        # partition table creation
        error_regex = "Failed to create partition table on /dev/sda"
        mocked_execute.side_effect = [
            processutils.ProcessExecutionError]
        self.assertRaisesRegex(errors.SoftwareRAIDError, error_regex,
                               self.hardware.create_configuration,
                               self.node, [])
        # partition creation
        error_regex = "Failed to create partitions on /dev/sda"
        mocked_execute.side_effect = [
            None,  # partition tables on sda
            ('42', None),  # sgdisk -F sda
            None,  # partition tables on sdb
            ('42', None),  # sgdisk -F sdb
            processutils.ProcessExecutionError]
        self.assertRaisesRegex(errors.SoftwareRAIDError, error_regex,
                               self.hardware.create_configuration,
                               self.node, [])
        # raid device creation
        error_regex = ("Failed to create md device /dev/md0 "
                       "on /dev/sda1 /dev/sdb1")
        mocked_execute.side_effect = [
            None,  # partition tables on sda
            ('42', None),  # sgdisk -F sda
            None,  # partition tables on sdb
            ('42', None),  # sgdisk -F sdb
            None, None, None, None,  # RAID-1 partitions on sd{a,b} + partx
            None, None, None, None,  # RAID-N partitions on sd{a,b} + partx
            processutils.ProcessExecutionError]
        self.assertRaisesRegex(errors.SoftwareRAIDError, error_regex,
                               self.hardware.create_configuration,
                               self.node, [])

    def test_create_configuration_empty_target_raid_config(self):
        self.node['target_raid_config'] = {}
        result = self.hardware.create_configuration(self.node, [])
        self.assertEqual(result, {})

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_with_nvme(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "10",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/nvme0n1', 'nvme0n1',
                                       107374182400, True)
        device2 = hardware.BlockDevice('/dev/nvme1n1', 'nvme1n1',
                                       107374182400, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.return_value = [device1, device2]

        mocked_execute.side_effect = [
            None,  # mklabel sda
            ("WARNING MBR NOT GPT\n42", None),  # sgdisk -F sda
            None,  # mklabel sda
            ("WARNING MBR NOT GPT\n42", None),  # sgdisk -F sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None,  # parted + partx sda
            None, None,  # parted + partx sdb
            None, None  # mdadms
        ]

        result = self.hardware.create_configuration(self.node, [])

        mocked_execute.assert_has_calls([
            mock.call('parted', '/dev/nvme0n1', '-s', '--', 'mklabel',
                      'msdos'),
            mock.call('sgdisk', '-F', '/dev/nvme0n1'),
            mock.call('parted', '/dev/nvme1n1', '-s', '--', 'mklabel',
                      'msdos'),
            mock.call('sgdisk', '-F', '/dev/nvme1n1'),
            mock.call('parted', '/dev/nvme0n1', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '10GiB'),
            mock.call('partx', '-u', '/dev/nvme0n1', check_exit_code=False),
            mock.call('parted', '/dev/nvme1n1', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '42s', '10GiB'),
            mock.call('partx', '-u', '/dev/nvme1n1', check_exit_code=False),
            mock.call('parted', '/dev/nvme0n1', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '10GiB', '-1'),
            mock.call('partx', '-u', '/dev/nvme0n1', check_exit_code=False),
            mock.call('parted', '/dev/nvme1n1', '-s', '-a', 'optimal', '--',
                      'mkpart', 'primary', '10GiB', '-1'),
            mock.call('partx', '-u', '/dev/nvme1n1', check_exit_code=False),
            mock.call('mdadm', '--create', '/dev/md0', '--force', '--run',
                      '--metadata=1', '--level', '1', '--raid-devices', 2,
                      '/dev/nvme0n1p1', '/dev/nvme1n1p1'),
            mock.call('mdadm', '--create', '/dev/md1', '--force', '--run',
                      '--metadata=1', '--level', '0', '--raid-devices', 2,
                      '/dev/nvme0n1p2', '/dev/nvme1n1p2')])
        self.assertEqual(raid_config, result)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_create_configuration_failure_with_nvme(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "100",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.node['target_raid_config'] = raid_config
        device1 = hardware.BlockDevice('/dev/nvme0n1', 'nvme0n1',
                                       107374182400, True)
        device2 = hardware.BlockDevice('/dev/nvme1n1', 'nvme1n1',
                                       107374182400, True)
        self.hardware.list_block_devices = mock.Mock()
        self.hardware.list_block_devices.side_effect = [
            [device1, device2],
            [device1, device2],
            [device1, device2],
            [device1, device2],
            [device1, device2],
            [device1, device2]]

        # partition table creation
        error_regex = "Failed to create partition table on /dev/nvme0n1"
        mocked_execute.side_effect = [
            processutils.ProcessExecutionError]
        self.assertRaisesRegex(errors.SoftwareRAIDError, error_regex,
                               self.hardware.create_configuration,
                               self.node, [])
        # partition creation
        error_regex = "Failed to create partitions on /dev/nvme0n1"
        mocked_execute.side_effect = [
            None,  # partition tables on sda
            ('42', None),  # sgdisk -F sda
            None,  # partition tables on sdb
            ('42', None),  # sgdisk -F sdb
            processutils.ProcessExecutionError]
        self.assertRaisesRegex(errors.SoftwareRAIDError, error_regex,
                               self.hardware.create_configuration,
                               self.node, [])
        # raid device creation
        error_regex = ("Failed to create md device /dev/md0 "
                       "on /dev/nvme0n1p1 /dev/nvme1n1p1")
        mocked_execute.side_effect = [
            None,  # partition tables on sda
            ('42', None),  # sgdisk -F sda
            None,  # partition tables on sdb
            ('42', None),  # sgdisk -F sdb
            None, None, None, None,  # RAID-1 partitions on sd{a,b} + partx
            None, None, None, None,  # RAID-N partitions on sd{a,b} + partx
            processutils.ProcessExecutionError]
        self.assertRaisesRegex(errors.SoftwareRAIDError, error_regex,
                               self.hardware.create_configuration,
                               self.node, [])

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__get_component_devices(self, mocked_execute):
        mocked_execute.side_effect = [(MDADM_DETAIL_OUTPUT, '')]
        component_devices = hardware._get_component_devices('/dev/md0')
        self.assertEqual(['/dev/vde1', '/dev/vdf1'], component_devices)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test__get_component_devices_broken_raid0(self, mocked_execute):
        mocked_execute.side_effect = [(MDADM_DETAIL_OUTPUT_BROKEN_RAID0, '')]
        component_devices = hardware._get_component_devices('/dev/md126')
        self.assertEqual(['/dev/sda2'], component_devices)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_holder_disks(self, mocked_execute):
        mocked_execute.side_effect = [(MDADM_DETAIL_OUTPUT, '')]
        holder_disks = hardware.get_holder_disks('/dev/md0')
        self.assertEqual(['/dev/vde', '/dev/vdf'], holder_disks)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_holder_disks_with_nvme(self, mocked_execute):
        mocked_execute.side_effect = [(MDADM_DETAIL_OUTPUT_NVME, '')]
        holder_disks = hardware.get_holder_disks('/dev/md0')
        self.assertEqual(['/dev/nvme0n1', '/dev/nvme1n1'], holder_disks)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_holder_disks_unexpected_devices(self, mocked_execute):
        side_effect = MDADM_DETAIL_OUTPUT_NVME.replace('nvme1n1p1',
                                                       'notmatching1a')
        mocked_execute.side_effect = [(side_effect, '')]
        self.assertRaisesRegex(
            errors.SoftwareRAIDError,
            r'^Software RAID caused unknown error: Could not get holder disks '
            r'of /dev/md0: unexpected pattern for partition '
            r'/dev/notmatching1a$',
            hardware.get_holder_disks, '/dev/md0')

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_holder_disks_broken_raid0(self, mocked_execute):
        mocked_execute.side_effect = [(MDADM_DETAIL_OUTPUT_BROKEN_RAID0, '')]
        holder_disks = hardware.get_holder_disks('/dev/md126')
        self.assertEqual(['/dev/sda'], holder_disks)

    @mock.patch.object(hardware, 'get_holder_disks', autospec=True)
    @mock.patch.object(hardware, '_get_component_devices', autospec=True)
    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_delete_configuration(self, mocked_execute, mocked_list,
                                  mocked_get_component, mocked_get_holder):
        raid_device1 = hardware.BlockDevice('/dev/md0', 'RAID-1',
                                            107374182400, True)
        raid_device2 = hardware.BlockDevice('/dev/md1', 'RAID-0',
                                            2147483648, True)
        sda = hardware.BlockDevice('/dev/sda', 'model12', 21, True)
        sdb = hardware.BlockDevice('/dev/sdb', 'model12', 21, True)
        sdc = hardware.BlockDevice('/dev/sdc', 'model12', 21, True)

        hardware.list_all_block_devices.side_effect = [
            [raid_device1, raid_device2],  # list_all_block_devices raid
            [],  # list_all_block_devices raid (md)
            [sda, sdb, sdc],  # list_all_block_devices disks
            [],  # list_all_block_devices parts
            [],  # list_all_block_devices raid
            [],  # list_all_block_devices raid (md)
        ]
        mocked_get_component.side_effect = [
            ["/dev/sda1", "/dev/sdb1"],
            ["/dev/sda2", "/dev/sdb2"]]
        mocked_get_holder.side_effect = [
            ["/dev/sda", "/dev/sdb"],
            ["/dev/sda", "/dev/sdb"]]
        mocked_execute.side_effect = [
            None,  # mdadm --assemble --scan
            None,  # wipefs md0
            None,  # mdadm --stop md0
            ['_', 'mdadm --examine output for sda1'],
            None,  # mdadm zero-superblock sda1
            ['_', 'mdadm --examine output for sdb1'],
            None,  # mdadm zero-superblock sdb1
            None,  # wipefs sda
            None,  # wipefs sdb
            None,  # wipfs md1
            None,  # mdadm --stop md1
            ['_', 'mdadm --examine output for sda2'],
            None,  # mdadm zero-superblock sda2
            ['_', 'mdadm --examine output for sdb2'],
            None,  # mdadm zero-superblock sdb2
            None,  # wipefs sda
            None,  # wipefs sda
            ['_', 'mdadm --examine output for sdc'],
            None,   # mdadm zero-superblock sdc
            # examine sdb
            processutils.ProcessExecutionError('No md superblock detected'),
            # examine sda
            processutils.ProcessExecutionError('No md superblock detected'),
            None,  # mdadm --assemble --scan
        ]

        self.hardware.delete_configuration(self.node, [])

        mocked_execute.assert_has_calls([
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
            mock.call('wipefs', '-af', '/dev/md0'),
            mock.call('mdadm', '--stop', '/dev/md0'),
            mock.call('mdadm', '--examine', '/dev/sda1',
                      use_standard_locale=True),
            mock.call('mdadm', '--zero-superblock', '/dev/sda1'),
            mock.call('mdadm', '--examine', '/dev/sdb1',
                      use_standard_locale=True),
            mock.call('mdadm', '--zero-superblock', '/dev/sdb1'),
            mock.call('wipefs', '-af', '/dev/sda'),
            mock.call('wipefs', '-af', '/dev/sdb'),
            mock.call('wipefs', '-af', '/dev/md1'),
            mock.call('mdadm', '--stop', '/dev/md1'),
            mock.call('mdadm', '--examine', '/dev/sda2',
                      use_standard_locale=True),
            mock.call('mdadm', '--zero-superblock', '/dev/sda2'),
            mock.call('mdadm', '--examine', '/dev/sdb2',
                      use_standard_locale=True),
            mock.call('mdadm', '--zero-superblock', '/dev/sdb2'),
            mock.call('wipefs', '-af', '/dev/sda'),
            mock.call('wipefs', '-af', '/dev/sdb'),
            mock.call('mdadm', '--examine', '/dev/sdc',
                      use_standard_locale=True),
            mock.call('mdadm', '--zero-superblock', '/dev/sdc'),
            mock.call('mdadm', '--examine', '/dev/sdb',
                      use_standard_locale=True),
            mock.call('mdadm', '--examine', '/dev/sda',
                      use_standard_locale=True),
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
        ])

    @mock.patch.object(hardware, '_get_component_devices', autospec=True)
    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_delete_configuration_partition(self, mocked_execute, mocked_list,
                                            mocked_get_component):
        # This test checks that if no components are returned for a given
        # raid device, then it must be a nested partition and so it gets
        # skipped
        raid_device1_part1 = hardware.BlockDevice('/dev/md0p1', 'RAID-1',
                                                  1073741824, True)
        hardware.list_all_block_devices.side_effect = [
            [],  # list_all_block_devices raid
            [raid_device1_part1],  # list_all_block_devices raid (md)
            [],  # list_all_block_devices disks
            [],  # list_all_block_devices parts
            [],  # list_all_block_devices raid
            [],  # list_all_block_devices raid (md)
        ]
        mocked_get_component.return_value = []
        self.assertIsNone(self.hardware.delete_configuration(self.node, []))
        mocked_execute.assert_has_calls([
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
        ])

    @mock.patch.object(hardware, '_get_component_devices', autospec=True)
    @mock.patch.object(hardware, 'list_all_block_devices', autospec=True)
    @mock.patch.object(utils, 'execute', autospec=True)
    def test_delete_configuration_failure_blocks_remaining(
            self, mocked_execute, mocked_list, mocked_get_component):

        # This test checks that, if after two raid clean passes there still
        # remain softraid hints on drives, then the delete_configuration call
        # raises an error
        raid_device1 = hardware.BlockDevice('/dev/md0', 'RAID-1',
                                            107374182400, True)

        hardware.list_all_block_devices.side_effect = [
            [raid_device1],  # list_all_block_devices raid
            [],  # list_all_block_devices raid (type md)
            [],  # list_all_block_devices disks
            [],  # list_all_block_devices parts
            [raid_device1],  # list_all_block_devices raid
            [],  # list_all_block_devices raid (type md)
            [],  # list_all_block_devices disks
            [],  # list_all_block_devices parts
            [raid_device1],  # list_all_block_devices raid
            [],  # list_all_block_devices raid (type md)
        ]
        mocked_get_component.return_value = []

        self.assertRaisesRegex(
            errors.SoftwareRAIDError,
            r"^Software RAID caused unknown error: Unable to clean all "
            r"softraid correctly. Remaining \['/dev/md0'\]$",
            self.hardware.delete_configuration, self.node, [])

        mocked_execute.assert_has_calls([
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
            mock.call('mdadm', '--assemble', '--scan', check_exit_code=False),
        ])

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_validate_configuration_valid_raid1(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "MAX",
                    "raid_level": "1",
                    "controller": "software",
                },
            ]
        }
        self.assertIsNone(self.hardware.validate_configuration(raid_config,
                                                               self.node))

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_validate_configuration_valid_raid1_raidN(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "100",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.assertIsNone(self.hardware.validate_configuration(raid_config,
                                                               self.node))

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_validate_configuration_invalid_MAX_MAX(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "MAX",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
            ]
        }
        self.assertRaises(errors.SoftwareRAIDError,
                          self.hardware.validate_configuration,
                          raid_config, self.node)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_validate_configuration_invalid_raid_level(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "MAX",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "42",
                    "controller": "software",
                },
            ]
        }
        self.assertRaises(errors.SoftwareRAIDError,
                          self.hardware.validate_configuration,
                          raid_config, self.node)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_validate_configuration_invalid_no_of_raids(self, mocked_execute):
        raid_config = {
            "logical_disks": [
                {
                    "size_gb": "MAX",
                    "raid_level": "1",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "0",
                    "controller": "software",
                },
                {
                    "size_gb": "MAX",
                    "raid_level": "1+0",
                    "controller": "software",
                },
            ]
        }
        self.assertRaises(errors.SoftwareRAIDError,
                          self.hardware.validate_configuration,
                          raid_config, self.node)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_system_vendor_info(self, mocked_execute):
        mocked_execute.return_value = LSHW_JSON_OUTPUT_V1
        vendor_info = self.hardware.get_system_vendor_info()
        self.assertEqual('ABC123 (GENERIC_SERVER)', vendor_info.product_name)
        self.assertEqual('1234567', vendor_info.serial_number)
        self.assertEqual('GENERIC', vendor_info.manufacturer)

    @mock.patch.object(utils, 'execute', autospec=True)
    def test_get_system_vendor_info_failure(self, mocked_execute):
        mocked_execute.side_effect = processutils.ProcessExecutionError()
        vendor_info = self.hardware.get_system_vendor_info()
        self.assertEqual('', vendor_info.product_name)
        self.assertEqual('', vendor_info.serial_number)
        self.assertEqual('', vendor_info.manufacturer)

    @mock.patch.object(utils, 'get_agent_params',
                       lambda: {'BOOTIF': 'boot:if'})
    @mock.patch.object(os.path, 'isdir', autospec=True)
    def test_get_boot_info_pxe_interface(self, mocked_isdir):
        mocked_isdir.return_value = False
        result = self.hardware.get_boot_info()
        self.assertEqual(hardware.BootInfo(current_boot_mode='bios',
                                           pxe_interface='boot:if'),
                         result)

    @mock.patch.object(os.path, 'isdir', autospec=True)
    def test_get_boot_info_bios(self, mocked_isdir):
        mocked_isdir.return_value = False
        result = self.hardware.get_boot_info()
        self.assertEqual(hardware.BootInfo(current_boot_mode='bios'), result)
        mocked_isdir.assert_called_once_with('/sys/firmware/efi')

    @mock.patch.object(os.path, 'isdir', autospec=True)
    def test_get_boot_info_uefi(self, mocked_isdir):
        mocked_isdir.return_value = True
        result = self.hardware.get_boot_info()
        self.assertEqual(hardware.BootInfo(current_boot_mode='uefi'), result)
        mocked_isdir.assert_called_once_with('/sys/firmware/efi')


@mock.patch.object(hardware.GenericHardwareManager,
                   'get_os_install_device', autospec=True)
@mock.patch.object(hardware, '_md_scan_and_assemble', autospec=True)
@mock.patch.object(hardware, '_check_for_iscsi', autospec=True)
@mock.patch.object(time, 'sleep', autospec=True)
class TestEvaluateHardwareSupport(base.IronicAgentTest):
    def setUp(self):
        super(TestEvaluateHardwareSupport, self).setUp()
        self.hardware = hardware.GenericHardwareManager()

    def test_evaluate_hw_waits_for_disks(
            self, mocked_sleep, mocked_check_for_iscsi,
            mocked_md_assemble, mocked_get_inst_dev):
        mocked_get_inst_dev.side_effect = [
            errors.DeviceNotFound('boom'),
            None
        ]

        result = self.hardware.evaluate_hardware_support()

        self.assertTrue(mocked_check_for_iscsi.called)
        self.assertTrue(mocked_md_assemble.called)
        self.assertEqual(hardware.HardwareSupport.GENERIC, result)
        mocked_get_inst_dev.assert_called_with(mock.ANY)
        self.assertEqual(2, mocked_get_inst_dev.call_count)
        mocked_sleep.assert_called_once_with(CONF.disk_wait_delay)

    @mock.patch.object(hardware, 'LOG', autospec=True)
    def test_evaluate_hw_no_wait_for_disks(
            self, mocked_log, mocked_sleep, mocked_check_for_iscsi,
            mocked_md_assemble, mocked_get_inst_dev):
        CONF.set_override('disk_wait_attempts', '0')

        result = self.hardware.evaluate_hardware_support()

        self.assertTrue(mocked_check_for_iscsi.called)
        self.assertEqual(hardware.HardwareSupport.GENERIC, result)
        self.assertFalse(mocked_get_inst_dev.called)
        self.assertFalse(mocked_sleep.called)
        self.assertFalse(mocked_log.called)

    @mock.patch.object(hardware, 'LOG', autospec=True)
    def test_evaluate_hw_waits_for_disks_nonconfigured(
            self, mocked_log, mocked_sleep, mocked_check_for_iscsi,
            mocked_md_assemble, mocked_get_inst_dev):
        mocked_get_inst_dev.side_effect = [
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            None
        ]

        self.hardware.evaluate_hardware_support()

        mocked_get_inst_dev.assert_called_with(mock.ANY)
        self.assertEqual(10, mocked_get_inst_dev.call_count)
        expected_calls = [mock.call(CONF.disk_wait_delay)] * 9
        mocked_sleep.assert_has_calls(expected_calls)
        mocked_log.warning.assert_called_once_with(
            'The root device was not detected in %d seconds',
            CONF.disk_wait_delay * 9)

    @mock.patch.object(hardware, 'LOG', autospec=True)
    def test_evaluate_hw_waits_for_disks_configured(self, mocked_log,
                                                    mocked_sleep,
                                                    mocked_check_for_iscsi,
                                                    mocked_md_assemble,
                                                    mocked_get_inst_dev):
        CONF.set_override('disk_wait_attempts', '1')

        mocked_get_inst_dev.side_effect = [
            errors.DeviceNotFound('boom'),
            errors.DeviceNotFound('boom'),
            None
        ]

        self.hardware.evaluate_hardware_support()

        mocked_get_inst_dev.assert_called_with(mock.ANY)
        self.assertEqual(1, mocked_get_inst_dev.call_count)
        self.assertFalse(mocked_sleep.called)
        mocked_log.warning.assert_called_once_with(
            'The root device was not detected')

    def test_evaluate_hw_disks_timeout_unconfigured(self, mocked_sleep,
                                                    mocked_check_for_iscsi,
                                                    mocked_md_assemble,
                                                    mocked_get_inst_dev):
        mocked_get_inst_dev.side_effect = errors.DeviceNotFound('boom')
        self.hardware.evaluate_hardware_support()
        mocked_sleep.assert_called_with(3)

    def test_evaluate_hw_disks_timeout_configured(self, mocked_sleep,
                                                  mocked_check_for_iscsi,
                                                  mocked_md_assemble,
                                                  mocked_root_dev):
        CONF.set_override('disk_wait_delay', '5')
        mocked_root_dev.side_effect = errors.DeviceNotFound('boom')

        self.hardware.evaluate_hardware_support()
        mocked_sleep.assert_called_with(5)

    def test_evaluate_hw_disks_timeout(
            self, mocked_sleep, mocked_check_for_iscsi,
            mocked_md_assemble, mocked_get_inst_dev):
        mocked_get_inst_dev.side_effect = errors.DeviceNotFound('boom')
        result = self.hardware.evaluate_hardware_support()
        self.assertEqual(hardware.HardwareSupport.GENERIC, result)
        mocked_get_inst_dev.assert_called_with(mock.ANY)
        self.assertEqual(CONF.disk_wait_attempts,
                         mocked_get_inst_dev.call_count)
        mocked_sleep.assert_called_with(CONF.disk_wait_delay)


@mock.patch.object(os, 'listdir', lambda *_: [])
@mock.patch.object(utils, 'execute', autospec=True)
class TestModuleFunctions(base.IronicAgentTest):

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(hardware, '_get_device_info',
                       lambda x, y, z: 'FooTastic')
    @mock.patch.object(hardware, '_udev_settle', autospec=True)
    @mock.patch.object(hardware.pyudev.Devices, "from_device_file",
                       autospec=False)
    def test_list_all_block_devices_success(self, mocked_fromdevfile,
                                            mocked_udev, mocked_readlink,
                                            mocked_execute):
        mocked_readlink.return_value = '../../sda'
        mocked_fromdevfile.return_value = {}
        mocked_execute.return_value = (BLK_DEVICE_TEMPLATE_SMALL, '')
        result = hardware.list_all_block_devices()
        mocked_execute.assert_called_once_with(
            'lsblk', '-Pbia', '-oKNAME,MODEL,SIZE,ROTA,TYPE',
            check_exit_code=[0])
        self.assertEqual(BLK_DEVICE_TEMPLATE_SMALL_DEVICES, result)
        mocked_udev.assert_called_once_with()

    @mock.patch.object(os, 'readlink', autospec=True)
    @mock.patch.object(hardware, '_get_device_info',
                       lambda x, y, z: 'FooTastic')
    @mock.patch.object(hardware, '_udev_settle', autospec=True)
    @mock.patch.object(hardware.pyudev.Devices, "from_device_file",
                       autospec=False)
    def test_list_all_block_devices_success_raid(self, mocked_fromdevfile,
                                                 mocked_udev, mocked_readlink,
                                                 mocked_execute):
        mocked_readlink.return_value = '../../sda'
        mocked_fromdevfile.return_value = {}
        mocked_execute.return_value = (RAID_BLK_DEVICE_TEMPLATE, '')
        result = hardware.list_all_block_devices(ignore_empty=False)
        mocked_execute.assert_called_once_with(
            'lsblk', '-Pbia', '-oKNAME,MODEL,SIZE,ROTA,TYPE',
            check_exit_code=[0])
        self.assertEqual(RAID_BLK_DEVICE_TEMPLATE_DEVICES, result)
        mocked_udev.assert_called_once_with()

    @mock.patch.object(hardware, '_get_device_info',
                       lambda x, y: "FooTastic")
    @mock.patch.object(hardware, '_udev_settle', autospec=True)
    def test_list_all_block_devices_wrong_block_type(self, mocked_udev,
                                                     mocked_execute):
        mocked_execute.return_value = ('TYPE="foo" MODEL="model"', '')
        result = hardware.list_all_block_devices()
        mocked_execute.assert_called_once_with(
            'lsblk', '-Pbia', '-oKNAME,MODEL,SIZE,ROTA,TYPE',
            check_exit_code=[0])
        self.assertEqual([], result)
        mocked_udev.assert_called_once_with()

    @mock.patch.object(hardware, '_udev_settle', autospec=True)
    def test_list_all_block_devices_missing(self, mocked_udev,
                                            mocked_execute):
        """Test for missing values returned from lsblk"""
        mocked_execute.return_value = ('TYPE="disk" MODEL="model"', '')
        self.assertRaisesRegex(
            errors.BlockDeviceError,
            r'^Block device caused unknown error: KNAME, ROTA, SIZE must be '
            r'returned by lsblk.$',
            hardware.list_all_block_devices)
        mocked_udev.assert_called_once_with()

    def test__udev_settle(self, mocked_execute):
        hardware._udev_settle()
        mocked_execute.assert_called_once_with('udevadm', 'settle')

    def test__check_for_iscsi(self, mocked_execute):
        hardware._check_for_iscsi()
        mocked_execute.assert_has_calls([
            mock.call('iscsistart', '-f'),
            mock.call('iscsistart', '-b')])

    def test__check_for_iscsi_no_iscsi(self, mocked_execute):
        mocked_execute.side_effect = processutils.ProcessExecutionError()
        hardware._check_for_iscsi()
        mocked_execute.assert_has_calls([
            mock.call('iscsistart', '-f')])


def create_hdparm_info(supported=False, enabled=False, locked=False,
                       frozen=False, enhanced_erase=False):

    def update_values(values, state, key):
        if not state:
            values[key] = 'not' + values[key]

    values = {
        'supported': '\tsupported',
        'enabled': '\tenabled',
        'locked': '\tlocked',
        'frozen': '\tfrozen',
        'enhanced_erase': '\tsupported: enhanced erase',
    }

    update_values(values, supported, 'supported')
    update_values(values, enabled, 'enabled')
    update_values(values, locked, 'locked')
    update_values(values, frozen, 'frozen')
    update_values(values, enhanced_erase, 'enhanced_erase')

    return HDPARM_INFO_TEMPLATE % values


@mock.patch('ironic_python_agent.hardware.dispatch_to_managers',
            autospec=True)
class TestListHardwareInfo(base.IronicAgentTest):

    def test_caching(self, mock_dispatch):
        fake_info = {'I am': 'hardware'}
        mock_dispatch.return_value = fake_info

        self.assertEqual(fake_info, hardware.list_hardware_info())
        self.assertEqual(fake_info, hardware.list_hardware_info())
        mock_dispatch.assert_called_once_with('list_hardware_info')

        self.assertEqual(fake_info,
                         hardware.list_hardware_info(use_cache=False))
        self.assertEqual(fake_info, hardware.list_hardware_info())
        mock_dispatch.assert_called_with('list_hardware_info')
        self.assertEqual(2, mock_dispatch.call_count)
