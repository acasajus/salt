'''
The static grains, these are the core, or built in grains.

When grains are loaded they are not loaded in the same way that modules are
loaded, grain functions are detected and executed, the functions MUST
return a dict which will be applied to the main grains dict. This module
will always be executed first, so that any grains loaded here in the core
module can be overwritten just by returning dict keys with the same value
as those returned here
'''

# This needs some refactoring, I made it "as fast as I could" and could be a
# lot clearer, so far it is spaghetti code
# Import python modules

import os
import socket
import sys
import re
import platform
import logging
import locale

# Extend the default list of supported distros. This will be used for the
# /etc/DISTRO-release checking that is part of platform.linux_distribution()
from platform import _supported_dists
_supported_dists += ('arch', 'mageia', 'meego', 'vmware', 'bluewhite64',
                     'slamd64', 'enterprise', 'ovs', 'system')

import salt.log
import salt.utils

# Solve the Chicken and egg problem where grains need to run before any
# of the modules are loaded and are generally available for any usage.
import salt.modules.cmdmod
__salt__ = {
    'cmd.run': salt.modules.cmdmod._run_quiet,
    'cmd.run_all': salt.modules.cmdmod._run_all_quiet
}

log = logging.getLogger(__name__)


def _windows_cpudata():
    '''
    Return the cpu information for Windows systems architecture
    '''
    # Provides:
    #   cpuarch
    #   cpu_model
    grains = {}
    if 'NUMBER_OF_PROCESSORS' in os.environ:
        # Cast to int so that the logic isn't broken when used as a
        # conditional in templating. Also follows _linux_cpudata()
        try:
            grains['num_cpus'] = int(os.environ['NUMBER_OF_PROCESSORS'])
        except ValueError:
            grains['num_cpus'] = 1
    grains['cpu_model'] = platform.processor()
    return grains


def _linux_cpudata():
    '''
    Return the cpu information for Linux systems architecture
    '''
    # Provides:
    #   num_cpus
    #   cpu_model
    #   cpu_flags
    grains = {}
    cpuinfo = '/proc/cpuinfo'
    # Parse over the cpuinfo file
    if os.path.isfile(cpuinfo):
        for line in open(cpuinfo, 'r').readlines():
            comps = line.split(':')
            if not len(comps) > 1:
                continue
            if comps[0].strip() == 'processor':
                grains['num_cpus'] = int(comps[1].strip()) + 1
            elif comps[0].strip() == 'model name':
                grains['cpu_model'] = comps[1].strip()
            elif comps[0].strip() == 'flags':
                grains['cpu_flags'] = comps[1].split()
    if 'num_cpus' not in grains:
        grains['num_cpus'] = 0
    if 'cpu_model' not in grains:
        grains['cpu_model'] = 'Unknown'
    if 'cpu_flags' not in grains:
        grains['cpu_flags'] = []
    return grains


def _bsd_cpudata(osdata):
    '''
    Return cpu information for BSD-like systems
    '''
    sysctl = salt.utils.which('sysctl')
    arch = salt.utils.which('arch')
    cmds = {}

    if sysctl:
        cmds['num_cpus'] = '{0} -n hw.ncpu'.format(sysctl)
        cmds['cpu_model'] = '{0} -n hw.model'.format(sysctl)
        cmds['cpuarch'] = '{0} -n hw.machine'.format(sysctl)
    if arch and osdata['kernel'] == 'OpenBSD':
        cmds['cpuarch'] = '{0} -s'.format(arch)

    grains = dict([(k, __salt__['cmd.run'](v)) for k, v in cmds.items()])
    grains['cpu_flags'] = []
    try:
        grains['num_cpus'] = int(grains['num_cpus'])
    except ValueError:
        grains['num_cpus'] = 1

    return grains


def _sunos_cpudata(osdata):
    '''
    Return the cpu information for Solaris-like systems
    '''
    # Provides:
    #   cpuarch
    #   num_cpus
    #   cpu_model
    grains = {'num_cpus': 0}

    grains['cpuarch'] = __salt__['cmd.run']('uname -p').strip()
    for line in __salt__['cmd.run']('/usr/sbin/psrinfo 2>/dev/null').split('\n'):
        grains['num_cpus'] += 1
    grains['cpu_model'] = __salt__['cmd.run']('kstat -p cpu_info:0:cpu_info0:implementation').split()[1].strip()

    return grains


def _memdata(osdata):
    '''
    Gather information about the system memory
    '''
    # Provides:
    #   mem_total
    grains = {'mem_total': 0}
    if osdata['kernel'] == 'Linux':
        meminfo = '/proc/meminfo'

        if os.path.isfile(meminfo):
            for line in open(meminfo, 'r').readlines():
                comps = line.split(':')
                if not len(comps) > 1:
                    continue
                if comps[0].strip() == 'MemTotal':
                    grains['mem_total'] = int(comps[1].split()[0]) / 1024
    elif osdata['kernel'] in ('FreeBSD', 'OpenBSD'):
        sysctl = salt.utils.which('sysctl')
        if sysctl:
            mem = __salt__['cmd.run']('{0} -n hw.physmem'.format(sysctl)).strip()
            grains['mem_total'] = str(int(mem) / 1024 / 1024)
    elif osdata['kernel'] == 'SunOS':
        for line in __salt__['cmd.run']('/usr/sbin/prtconf 2>/dev/null').split('\n'):
            comps = line.split(' ')
            if comps[0].strip() == 'Memory' and comps[1].strip() == 'size:':
                grains['mem_total'] = int(comps[2].strip())
    elif osdata['kernel'] == 'Windows':
        for line in __salt__['cmd.run']('SYSTEMINFO /FO LIST').split('\n'):
            comps = line.split(':')
            if not len(comps) > 1:
                continue
            if comps[0].strip() == 'Total Physical Memory':
                # Windows XP use '.' as separator and Windows 2008 Server R2 use ','
                grains['mem_total'] = int(comps[1].split()[0].replace('.', '').replace(',', ''))
                break
    return grains


def _virtual(osdata):
    '''
    Returns what type of virtual hardware is under the hood, kvm or physical
    '''
    # This is going to be a monster, if you are running a vm you can test this
    # grain with please submit patches!
    # Provides:
    #   virtual
    grains = {'virtual': 'physical'}
    lspci = salt.utils.which('lspci')
    dmidecode = salt.utils.which('dmidecode')

    for command in ('dmidecode', 'lspci'):
        which = salt.utils.which(command)

        if which is None:
            continue

        ret = __salt__['cmd.run_all'](which)

        if ret['retcode'] > 0:
            if salt.log.is_logging_configured():
                log.warn(
                    'Although \'{0}\' was found in path, the current user '
                    'cannot execute it. Grains output might not be '
                    'accurate.'.format(command)
                )
            continue

        output = ret['stdout']

        if command == 'dmidecode':
            # Product Name: VirtualBox
            if 'Vendor: QEMU' in output:
                # FIXME: Make this detect between kvm or qemu
                grains['virtual'] = 'kvm'
            if 'Vendor: Bochs' in output:
                grains['virtual'] = 'kvm'
            elif 'VirtualBox' in output:
                grains['virtual'] = 'VirtualBox'
            # Product Name: VMware Virtual Platform
            elif 'VMware' in output:
                grains['virtual'] = 'VMware'
            # Manufacturer: Microsoft Corporation
            # Product Name: Virtual Machine
            elif 'Manufacturer: Microsoft' in output and 'Virtual Machine' in output:
                grains['virtual'] = 'VirtualPC'
            # Manufacturer: Parallels Software International Inc.
            elif 'Parallels Software' in output:
                grains['virtual'] = 'Parallels'
            # Break out of the loop, lspci parsing is not necessary
            break
        elif command == 'lspci':
            # dmidecode not available or the user does not have the necessary
            # permissions
            model = output.lower()
            if 'vmware' in model:
                grains['virtual'] = 'VMware'
            # 00:04.0 System peripheral: InnoTek Systemberatung GmbH VirtualBox Guest Service
            elif 'virtualbox' in model:
                grains['virtual'] = 'VirtualBox'
            elif 'qemu' in model:
                grains['virtual'] = 'kvm'
            elif 'virtio' in model:
                grains['virtual'] = 'kvm'
            # Break out of the loop so the next log message is not issued
            break
    else:
        log.warn(
            'Both \'dmidecode\' and \'lspci\' failed to execute, either '
            'because they do not exist on the system of the user running '
            'this instance does not have the necessary permissions to '
            'execute them. Grains output might not be accurate.'
        )

    choices = ('Linux', 'OpenBSD', 'HP-UX')
    isdir = os.path.isdir
    if osdata['kernel'] in choices:
        if isdir('/proc/vz'):
            if os.path.isfile('/proc/vz/version'):
                grains['virtual'] = 'openvzhn'
            else:
                grains['virtual'] = 'openvzve'
        elif isdir('/proc/sys/xen') or isdir('/sys/bus/xen') or isdir('/proc/xen'):
            if os.path.isfile('/proc/xen/xsd_kva'):
                # Tested on CentOS 5.3 / 2.6.18-194.26.1.el5xen
                # Tested on CentOS 5.4 / 2.6.18-164.15.1.el5xen
                grains['virtual_subtype'] = 'Xen Dom0'
            else:
                if grains.get('productname', '') == 'HVM domU':
                    # Requires dmidecode!
                    grains['virtual_subtype'] = 'Xen HVM DomU'
                elif os.path.isfile('/proc/xen/capabilities') and os.access('/proc/xen/capabilities', os.R_OK):
                    caps = open('/proc/xen/capabilities')
                    if 'control_d' not in caps.read():
                        # Tested on CentOS 5.5 / 2.6.18-194.3.1.el5xen
                        grains['virtual_subtype'] = 'Xen PV DomU'
                    else:
                        # Shouldn't get to this, but just in case
                        grains['virtual_subtype'] = 'Xen Dom0'
                    caps.close()
                # Tested on Fedora 10 / 2.6.27.30-170.2.82 with xen
                # Tested on Fedora 15 / 2.6.41.4-1 without running xen
                elif isdir('/sys/bus/xen'):
                    if 'xen' in __salt__['cmd.run']('dmesg').lower():
                        grains['virtual_subtype'] = 'Xen PV DomU'
                    elif os.listdir('/sys/bus/xen/drivers'):
                        # An actual DomU will have several drivers
                        # whereas a paravirt ops kernel will  not.
                        grains['virtual_subtype'] = 'Xen PV DomU'
            # If a Dom0 or DomU was detected, obviously this is xen
            if 'dom' in grains.get('virtual_subtype', '').lower():
                grains['virtual'] = 'xen'
        if os.path.isfile('/proc/cpuinfo'):
            if 'QEMU Virtual CPU' in open('/proc/cpuinfo', 'r').read():
                grains['virtual'] = 'kvm'
    elif osdata['kernel'] == 'FreeBSD':
        sysctl = salt.utils.which('sysctl')
        kenv = salt.utils.which('kenv')
        if kenv:
            product = __salt__['cmd.run']('{0} smbios.system.product'.format(kenv)).strip()
            if product.startswith('VMware'):
                grains['virtual'] = 'VMware'
        if sysctl:
            model = __salt__['cmd.run']('{0} hw.model'.format(sysctl)).strip()
            jail = __salt__['cmd.run']('{0} -n security.jail.jailed'.format(sysctl)).strip()
            if jail:
                grains['virtual_subtype'] = 'jail'
            if 'QEMU Virtual CPU' in model:
                grains['virtual'] = 'kvm'
    elif osdata['kernel'] == 'SunOS':
        # Check if it's a "regular" zone. (i.e. Solaris 10/11 zone)
        zonename = salt.utils.which('zonename')
        if zonename:
            zone = __salt__['cmd.run']('{0}'.format(zonename)).strip()
            if zone != "global":
                grains['virtual'] = 'zone'
        # Check if it's a branded zone (i.e. Solaris 8/9 zone)
        if isdir('/.SUNWnative'):
            grains['virtual'] = 'zone'
    return grains


def _ps(osdata):
    '''
    Return the ps grain
    '''
    grains = {}
    bsd_choices = ('FreeBSD', 'NetBSD', 'OpenBSD', 'MacOS')
    if osdata['os'] in bsd_choices:
        grains['ps'] = 'ps auxwww'
    elif osdata['os'] == 'Solaris':
        grains['ps'] = '/usr/ucb/ps auxwww'
    elif osdata['os'] == 'Windows':
        grains['ps'] = 'tasklist.exe'
    elif osdata.get('virtual', '') == 'openvzhn':
        grains['ps'] = 'vzps -E 0 -efH|cut -b 6-'
    else:
        grains['ps'] = 'ps -efH'
    return grains

def _windows_platform_data(osdata):
    '''
    Use the platform module for as much as we can.
    '''
    # Provides:
    #    osmanufacturer
    #    manufacturer
    #    productname
    #    biosversion
    #    osfullname
    #    timezone
    #    windowsdomain

    import wmi
    from datetime import datetime
    wmi_c = wmi.WMI()
    # http://msdn.microsoft.com/en-us/library/windows/desktop/aa394102%28v=vs.85%29.aspx
    systeminfo = wmi_c.Win32_ComputerSystem()[0]
    # http://msdn.microsoft.com/en-us/library/windows/desktop/aa394239%28v=vs.85%29.aspx
    osinfo = wmi_c.Win32_OperatingSystem()[0]
    # http://msdn.microsoft.com/en-us/library/windows/desktop/aa394077(v=vs.85).aspx
    biosinfo = wmi_c.Win32_BIOS()[0]
    # http://msdn.microsoft.com/en-us/library/windows/desktop/aa394498(v=vs.85).aspx
    timeinfo = wmi_c.Win32_TimeZone()[0]

    # the name of the OS comes with a bunch of other data about the install
    # location. For example:
    # 'Microsoft Windows Server 2008 R2 Standard |C:\\Windows|\\Device\\Harddisk0\\Partition2'
    (osfullname, _) = osinfo.Name.split('|', 1)
    osfullname = osfullname.strip()
    grains = {
        'osmanufacturer': osinfo.Manufacturer,
        'manufacturer': systeminfo.Manufacturer,
        'productname': systeminfo.Model,
        # bios name had a bunch of whitespace appended to it in my testing
        # 'PhoenixBIOS 4.0 Release 6.0     '
        'biosversion': biosinfo.Name.strip(),
        'osfullname': osfullname,
        'timezone': timeinfo.Description,
        'windowsdomain': systeminfo.Domain,
    }

    return grains


def id_():
    '''
    Return the id
    '''
    return {'id': __opts__['id']}

# This maps (at most) the first ten characters (no spaces, lowercased) of
# 'osfullname' to the 'os' grain that Salt traditionally uses.
_os_name_map = {
    'redhatente': 'RedHat',
    'debian': 'Debian',
    'arch': 'Arch',
}

# Map the 'os' grain to the 'os_family' grain
_os_family_map = {
    'Ubuntu': 'Debian',
    'Fedora': 'RedHat',
    'CentOS': 'RedHat',
    'GoOSe': 'RedHat',
    'Scientific': 'RedHat',
    'Amazon': 'RedHat',
    'CloudLinux': 'RedHat',
    'Mandrake': 'Mandriva',
    'ESXi': 'VMWare',
    'VMWareESX': 'VMWare',
    'Bluewhite64': 'Bluewhite',
    'Slamd64': 'Slackware',
    'OVS': 'Oracle',
    'OEL': 'Oracle',
    'SLES': 'Suse',
    'SLED': 'Suse',
    'openSUSE': 'Suse',
    'SUSE': 'Suse'
}


def os_data():
    '''
    Return grains pertaining to the operating system
    '''
    grains = {}
    (grains['defaultlanguage'],
     grains['defaultencoding']) = locale.getdefaultlocale()
    # Windows Server 2008 64-bit
    # ('Windows', 'MINIONNAME', '2008ServerR2', '6.1.7601', 'AMD64', 'Intel64 Fam ily 6 Model 23 Stepping 6, GenuineIntel')
    # Ubuntu 10.04
    # ('Linux', 'MINIONNAME', '2.6.32-38-server', '#83-Ubuntu SMP Wed Jan 4 11:26:59 UTC 2012', 'x86_64', '')
    (grains['kernel'], grains['host'],
     grains['kernelrelease'], version, grains['cpuarch'], _) = platform.uname()
    if grains['kernel'] == 'Windows':
        grains['osrelease'] = grains['kernelrelease']
        grains['osversion'] = grains['kernelrelease'] = version
        grains['os'] = 'Windows'
        grains['os_family'] = 'Windows'
        grains.update(_memdata(grains))
        grains.update(_windows_platform_data(grains))
        grains.update(_windows_cpudata())
        grains.update(_ps(grains))
        return grains
    elif grains['kernel'] == 'Linux':
        # Add lsb grains on any distro with lsb-release
        try:
            import lsb_release
            release = lsb_release.get_distro_information()
            for key, value in release.iteritems():
                grains['lsb_{0}'.format(key.lower())] = value  # override /etc/lsb-release
        except ImportError:
            # if the python library isn't available, default to regex
            if os.path.isfile('/etc/lsb-release'):
                for line in open('/etc/lsb-release').readlines():
                    # Matches any possible format:
                    #     DISTRIB_ID="Ubuntu"
                    #     DISTRIB_ID='Mageia'
                    #     DISTRIB_ID=Fedora
                    #     DISTRIB_RELEASE='10.10'
                    #     DISTRIB_CODENAME='squeeze'
                    #     DISTRIB_DESCRIPTION='Ubuntu 10.10'
                    regex = re.compile('^(DISTRIB_(?:ID|RELEASE|CODENAME|DESCRIPTION))=(?:\'|")?([\w\s\.-_]+)(?:\'|")?')
                    match = regex.match(line)
                    if match:
                        # Adds: lsb_distrib_{id,release,codename,description}
                        grains['lsb_{0}'.format(match.groups()[0].lower())] = match.groups()[1].rstrip()
        # Use the already intelligent platform module to get distro info
        (osname, osrelease, oscodename) = platform.linux_distribution(
                                              supported_dists=_supported_dists)
        # Try to assign these three names based on the lsb info, they tend to
        # be more accurate than what python gets from /etc/DISTRO-release.
        # It's worth noting that Ubuntu has patched their Python distribution
        # so that platform.linux_distribution() does the /etc/lsb-release
        # parsing, but we do it anyway here for the sake for full portability.
        grains['osfullname'] = grains.get('lsb_distrib_id', osname)
        grains['osrelease'] = grains.get('lsb_distrib_release', osrelease)
        grains['oscodename'] = grains.get('lsb_distrib_codename', oscodename)
        # return the first ten characters with no spaces, lowercased
        shortname = grains['osfullname'].replace(' ', '').lower()[:10]
        # this maps the long names from the /etc/DISTRO-release files to the
        # traditional short names that Salt has used.
        grains['os'] = _os_name_map.get(shortname, grains['osfullname'])
        grains.update(_linux_cpudata())
    elif grains['kernel'] == 'SunOS':
        grains['os'] = 'Solaris'
        grains.update(_sunos_cpudata(grains))
    elif grains['kernel'] == 'VMkernel':
        grains['os'] = 'ESXi'
    elif grains['kernel'] == 'Darwin':
        grains['os'] = 'MacOS'
        grains.update(_bsd_cpudata(grains))
    else:
        grains['os'] = grains['kernel']
    if grains['kernel'] in ('FreeBSD', 'OpenBSD'):
        grains.update(_bsd_cpudata(grains))
    if not grains['os']:
        grains['os'] = 'Unknown {0}'.format(grains['kernel'])
        grains['os_family'] = 'Unknown'
    else:
        # this assigns family names based on the os name
        # family defaults to the os name if not found
        grains['os_family'] = _os_family_map.get(grains['os'],
                                                 grains['os'])

    grains.update(_memdata(grains))

    # Get the hardware and bios data
    grains.update(_hw_data(grains))

    # Load the virtual machine info
    grains.update(_virtual(grains))
    grains.update(_ps(grains))

    return grains


def hostname():
    '''
    Return fqdn, hostname, domainname
    '''
    # This is going to need some work
    # Provides:
    #   fqdn
    #   host
    #   localhost
    #   domain
    grains = {}
    grains['localhost'] = socket.gethostname()
    grains['fqdn'] = socket.getfqdn()
    (grains['host'], grains['domain']) = grains['fqdn'].partition('.')[::2]
    return grains


def path():
    '''
    Return the path
    '''
    # Provides:
    #   path
    return {'path': os.environ['PATH'].strip()}


def pythonversion():
    '''
    Return the Python version
    '''
    # Provides:
    #   pythonversion
    return {'pythonversion': list(sys.version_info)}


def pythonpath():
    '''
    Return the Python path
    '''
    # Provides:
    #   pythonpath
    return {'pythonpath': sys.path}


def saltpath():
    '''
    Return the path of the salt module
    '''
    # Provides:
    #   saltpath
    path = os.path.abspath(os.path.join(__file__, os.path.pardir))
    return {'saltpath': os.path.dirname(path)}


def saltversion():
    '''
    Return the version of salt
    '''
    # Provides:
    #   saltversion
    from salt import __version__
    return {'saltversion': __version__}


# Relatively complex mini-algorithm to iterate over the various
# sections of dmidecode output and return matches for  specific
# lines containing data we want, but only in the right section.
def _dmidecode_data(regex_dict):
    '''
    Parse the output of dmidecode in a generic fashion that can
    be used for the multiple system types which have dmidecode.
    '''
    # NOTE: This function might gain support for smbios instead
    #       of dmidecode when salt gets working Solaris support
    ret = {}

    # No use running if dmidecode isn't in the path
    if not salt.utils.which('dmidecode'):
        return ret

    out = __salt__['cmd.run']('dmidecode')

    for section in regex_dict:
        section_found = False

        # Look at every line for the right section
        for line in out.split('\n'):
            if not line:
                continue
            # We've found it, woohoo!
            if re.match(section, line):
                section_found = True
                continue
            if not section_found:
                continue

            # Now that a section has been found, find the data
            for item in regex_dict[section]:
                # Examples:
                #    Product Name: 64639SU
                #    Version: 7LETC1WW (2.21 )
                regex = re.compile('\s+{0}\s+(.*)$'.format(item))
                grain = regex_dict[section][item]
                # Skip to the next iteration if this grain
                # has been found in the dmidecode output.
                if grain in ret:
                    continue

                match = regex.match(line)

                # Finally, add the matched data to the grains returned
                if match:
                    ret[grain] = match.group(1).strip()
    return ret


def _hw_data(osdata):
    '''
    Get system specific hardware data from dmidecode

    Provides
        biosversion
        productname
        manufacturer
        serialnumber
        biosreleasedate

    .. versionadded:: 0.9.5
    '''
    grains = {}
    # TODO: *BSD dmidecode output
    if osdata['kernel'] == 'Linux':
        linux_dmi_regex = {
            'BIOS [Ii]nformation': {
                '[Vv]ersion:': 'biosversion',
                '[Rr]elease [Dd]ate:': 'biosreleasedate',
            },
            '[Ss]ystem [Ii]nformation': {
                'Manufacturer:': 'manufacturer',
                'Product(?: Name)?:': 'productname',
                'Serial Number:': 'serialnumber',
            },
        }
        grains.update(_dmidecode_data(linux_dmi_regex))
    # On FreeBSD /bin/kenv (already in base system) can be used instead of dmidecode
    elif osdata['kernel'] == 'FreeBSD':
        kenv = salt.utils.which('kenv')
        if kenv:
            grains['biosreleasedate'] = __salt__['cmd.run']('{0} smbios.bios.reldate'.format(kenv)).strip()
            grains['biosversion'] = __salt__['cmd.run']('{0} smbios.bios.version'.format(kenv)).strip()
            grains['manufacturer'] = __salt__['cmd.run']('{0} smbios.system.maker'.format(kenv)).strip()
            grains['serialnumber'] = __salt__['cmd.run']('{0} smbios.system.serial'.format(kenv)).strip()
            grains['productname'] = __salt__['cmd.run']('{0} smbios.system.product'.format(kenv)).strip()
    elif osdata['kernel'] == 'OpenBSD':
        sysctl = salt.utils.which('sysctl')
        hwdata = {'biosversion': 'hw.version',
                  'manufacturer': 'hw.vendor',
                  'productname': 'hw.product',
                  'serialnumber': 'hw.serialno'}
        for key, oid in hwdata.items():
            value = __salt__['cmd.run']('{0} -n {1}'.format(sysctl, oid))
            if not value.endswith(' value is not available'):
                grains[key] = value
    return grains


def get_server_id():
    '''
    Provides an integer based on the FQDN of a machine.
    Useful as server-id in MySQL replication or anywhere else you'll need an ID like this.
    '''
    # Provides:
    #   server_id
    return {'server_id': abs(hash(__opts__['id']) % (2 ** 31))}
