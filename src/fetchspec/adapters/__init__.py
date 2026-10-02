"""Small product adapters: official scope, identity and vendor-specific parsing."""
from .asteralabs import AsteraLabsProductAdapter
from .delta import DeltaProductAdapter
from .micron import MicronProductAdapter
from .nvidia import NvidiaProductAdapter
from .siemens import SiemensProductAdapter
from .siemens_energy import SiemensEnergyProductAdapter
from .supermicro import SupermicroProductAdapter
from .vertiv import VertivProductAdapter


ADAPTERS = {'asteralabs': AsteraLabsProductAdapter, 'delta': DeltaProductAdapter, 'micron': MicronProductAdapter, 'nvidia': NvidiaProductAdapter, 'siemens': SiemensProductAdapter, 'siemens-energy': SiemensEnergyProductAdapter, 'supermicro': SupermicroProductAdapter, 'vertiv': VertivProductAdapter}


from .compute import ComputeProductAdapter, definitions
for _company in {entry['company_id'] for entry in definitions()['products']}:
    ADAPTERS[_company] = type('ReviewedComputeAdapter', (ComputeProductAdapter,), {'company_id': _company})


def adapter_for(company_id, known_catalog=None):
    if company_id not in ADAPTERS:
        raise ValueError('unsupported product adapter: ' + company_id)
    return ADAPTERS[company_id](known_catalog=known_catalog)
