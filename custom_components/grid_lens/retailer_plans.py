"""Retailer plan classes for electricity plan comparison."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, time
from typing import Dict, List, Tuple, Optional

_LOGGER = logging.getLogger(__name__)


class RetailerPlan(ABC):
    """Base class for electricity retailer plans."""
    
    def __init__(self):
        """Initialize the plan."""
        self.retailer = ""
        self.plan_name = ""
        self.daily_supply_charge = 0.0
        self.feed_in_tariff = 0.05  # Default feed-in rate
        self.demand_charge_per_kw_per_day = 0.0  # Demand charge ($/kW/day)
        self.is_market_linked = False  # True for wholesale plans (Amber, Flow Power)
        self.spot_export_pricing = False  # True when export credit follows spot price (Amber only)
        self.demand_charge_window = None  # Time window for demand measurement (start_hour, end_hour)
        # Fixed daily credit from VPP participation (e.g. guaranteed quarterly credits).
        # Subtracted from total bill after energy charges are computed.
        self.fixed_daily_credit = 0.0
    
    @abstractmethod
    def get_import_rate(self, dt: datetime) -> float:
        """Get the import rate ($/kWh) for a specific datetime.
        
        Args:
            dt: Datetime to get rate for
            
        Returns:
            Import rate in $/kWh
        """
        pass
    
    def get_export_rate(self, dt: datetime) -> float:
        """Get the export rate ($/kWh) for a specific datetime.
        
        Most plans have fixed feed-in tariffs. Override for time-varying.
        
        Args:
            dt: Datetime to get rate for
            
        Returns:
            Export rate in $/kWh
        """
        return self.feed_in_tariff
    
    @abstractmethod
    def describe_strategy(self) -> str:
        """Describe the optimal battery strategy for this plan.
        
        Returns:
            Human-readable description of strategy
        """
        pass
    
    @abstractmethod
    def get_display_breakdown(self, optimization_result: Dict, days: float = 30) -> Dict:
        """Get itemized cost breakdown for display.
        
        Args:
            optimization_result: Result from battery optimizer
            days: Number of days in billing period
            
        Returns:
            Dict with breakdown for display:
            {
                'sections': [
                    {
                        'title': 'Peak Usage (6am-10pm)',
                        'kwh': 45.2,
                        'rate': 0.32,
                        'cost': 14.46,
                    },
                    ...
                ],
                'total_energy_cost': 123.45,
                'supply_charge': 33.00,
                'demand_charge': 15.00,  # If applicable
                'total': 156.45,
            }
        """
        pass
    
    def get_plan_info(self) -> Dict:
        """Get basic plan information.
        
        Returns:
            Dict with plan details
        """
        return {
            'retailer': self.retailer,
            'plan_name': self.plan_name,
            'daily_supply_charge': self.daily_supply_charge,
            'feed_in_tariff': self.feed_in_tariff,
        }


class AmberPlan(RetailerPlan):
    """Amber Electric - Real-time wholesale pricing.

    Two distinct fixed charges modelled separately:
      daily_supply_charge: Ausgrid network pass-through (~105.45c/day).
        Verify from your Amber bill — figure is distributor-specific.
      monthly_subscription_fee: $25/month flat (Amber's membership fee).
    Both are added to the plan cost by plan_calculator.
    """

    MONTHLY_SUBSCRIPTION = 25.0  # $/month

    def __init__(self):
        super().__init__()
        self.retailer = "Amber Electric"
        self.plan_name = "SmartShift"
        # Ausgrid network daily supply charge (pass-through, no retailer margin).
        # Estimated from WATTever combined figure (187.54c) minus subscription equivalent.
        # Verify against your actual Amber bill and update if wrong.
        self.daily_supply_charge = 1.0545
        self.monthly_subscription_fee = self.MONTHLY_SUBSCRIPTION
        self.feed_in_tariff = 0.05  # Variable, but we'll use estimates
        self.is_market_linked = True
        self.spot_export_pricing = True  # Export credit follows spot price
    
    def get_import_rate(self, dt: datetime) -> float:
        """Amber uses real-time wholesale rates - we estimate for comparison."""
        # This should ideally pull from actual Amber price sensor
        # For now, use time-of-day estimates
        hour = dt.hour
        
        # Rough estimates based on typical wholesale patterns
        if 6 <= hour < 9 or 17 <= hour < 21:  # Morning/evening peaks
            return 0.25
        elif 10 <= hour < 16:  # Solar shoulder
            return 0.08
        else:  # Night
            return 0.12
        
        # Note: Real Amber rates vary constantly - this is just for comparison
    
    def describe_strategy(self) -> str:
        """Describe battery strategy."""
        return (
            "With Amber's real-time pricing, optimal battery strategy is:\n"
            "• Charge during solar hours (low/negative prices)\n"
            "• Discharge during evening peak (high prices)\n"
            "• Watch for spike hours and discharge aggressively\n"
            "• Your actual behavior (via EMHASS) likely already does this!"
        )
    
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        """Get cost breakdown."""
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30
        
        # Group by rate category
        peak_import = 0
        peak_cost = 0
        shoulder_import = 0
        shoulder_cost = 0
        offpeak_import = 0
        offpeak_cost = 0
        total_export_kwh = 0
        total_export_credit = 0
        
        for hour_data in schedule:
            hour = hour_data.get('hour', 0) % 24
            import_kwh = hour_data.get('import_kwh', 0)
            import_cost = hour_data.get('import_cost', 0)
            export_kwh = hour_data.get('export_kwh', 0)
            export_credit = hour_data.get('export_credit', 0)
            
            total_export_kwh += export_kwh
            total_export_credit += export_credit
            
            if 6 <= hour < 9 or 17 <= hour < 21:
                peak_import += import_kwh
                peak_cost += import_cost
            elif 10 <= hour < 16:
                shoulder_import += import_kwh
                shoulder_cost += import_cost
            else:
                offpeak_import += import_kwh
                offpeak_cost += import_cost
        
        sections = [
            {'title': 'Peak Hours (6-9am, 5-9pm)', 'kwh': round(peak_import, 2),
             'rate': round(peak_cost / peak_import, 3) if peak_import > 0 else 0.25,
             'cost': round(peak_cost, 2)},
            {'title': 'Solar Hours (10am-4pm)', 'kwh': round(shoulder_import, 2),
             'rate': round(shoulder_cost / shoulder_import, 3) if shoulder_import > 0 else 0.08,
             'cost': round(shoulder_cost, 2)},
            {'title': 'Off-Peak (Night)', 'kwh': round(offpeak_import, 2),
             'rate': round(offpeak_cost / offpeak_import, 3) if offpeak_import > 0 else 0.12,
             'cost': round(offpeak_cost, 2)},
        ]
        if total_export_kwh > 0:
            sections.append({
                'title': 'Solar Export (Credit)',
                'kwh': round(total_export_kwh, 2),
                'rate': round(total_export_credit / total_export_kwh, 3) if total_export_kwh > 0 else 0,
                'cost': round(-total_export_credit, 2),
            })
        
        supply_charge = self.daily_supply_charge * days
        subscription = self.monthly_subscription_fee * (days / 30.44)
        total_energy = optimization_result.get('net_cost', 0)

        return {
            'sections': sections,
            'total_energy_cost': round(total_energy, 2),
            'supply_charge': round(supply_charge, 2),
            'subscription_fee': round(subscription, 2),
            'total': round(total_energy + supply_charge + subscription, 2),
            'days': round(days, 1),
        }


class OVOEVPlan(RetailerPlan):
    """OVO Energy – The EV Plan.

    Rates sourced directly from OVO website, June 2026 (Ausgrid network):
      Supply: 108.9c/day
      Standard anytime: 40.315c/kWh (peak = off-peak, so effectively flat)
      EV off-peak: 10c/kWh (assumed 10pm-7am; confirm exact hours with OVO)
      CL1 (controlled load): 27.5c/kWh — not modelled separately
      Peak demand charge: 42.343c/kW/day — NOTE: field is set but the
        calculator does not yet compute demand charges; this understates
        the true cost for high-peak-demand households.
      FiT: 2.8c/kWh
    """

    STANDARD_RATE  = 0.40315  # $/kWh anytime (peak = off-peak)
    EV_OFFPEAK_RATE = 0.10    # $/kWh — cheap EV/battery charging window
    # EV off-peak hours: assumed 10pm-7am — CONFIRM WITH OVO
    EV_OFFPEAK_START = 0
    EV_OFFPEAK_END   = 6
    FIT_RATE         = 0.028  # $/kWh

    def __init__(self):
        super().__init__()
        self.retailer = "OVO Energy"
        self.plan_name = "The EV Plan"
        self.daily_supply_charge = 1.089
        self.feed_in_tariff = self.FIT_RATE
        self.demand_charge_per_kw_per_day = 0.42343  # set but not computed by calculator

    def get_import_rate(self, dt: datetime) -> float:
        h = dt.hour
        if self.EV_OFFPEAK_START <= h < self.EV_OFFPEAK_END:
            return self.EV_OFFPEAK_RATE
        return self.STANDARD_RATE

    def describe_strategy(self) -> str:
        return (
            "OVO The EV Plan strategy:\n"
            "• Flat 40.3c/kWh standard rate all hours outside midnight-6am (no TOU arbitrage)\n"
            "• Cheap off-peak: 10c/kWh midnight-6am — charge battery and EV then\n"
            "• 30c arbitrage: charge at 10c, discharge during day and evening at 40.3c\n"
            "• Discharge whenever grid import would otherwise occur — day or evening\n"
            "• DEMAND CHARGE: 42.3c/kW/day — minimise peak kW draw from grid\n"
            "  Battery helps reduce peak demand significantly\n"
            "• Very low FiT (2.8c) — prioritise self-consumption over export\n"
            "• NOTE: demand charge is not yet included in cost calculation"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        ev_import = standard_import = 0.0
        ev_cost = standard_cost = 0.0
        total_export_kwh = total_export_credit = 0.0

        for slot in schedule:
            h = slot.get('hour', 0) % 24
            imp = slot.get('import_kwh', 0.0)
            ic = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            ec = slot.get('export_credit', 0.0)
            total_export_kwh += exp
            total_export_credit += ec
            if self.EV_OFFPEAK_START <= h < self.EV_OFFPEAK_END:
                ev_import += imp; ev_cost += ic
            else:
                standard_import += imp; standard_cost += ic

        sections = [
            {
                'title': 'EV Off-Peak (midnight-6am)',
                'kwh': round(ev_import, 2),
                'rate': self.EV_OFFPEAK_RATE,
                'cost': round(ev_cost, 2),
            },
            {
                'title': 'Standard Rate (all other hours)',
                'kwh': round(standard_import, 2),
                'rate': self.STANDARD_RATE,
                'cost': round(standard_cost, 2),
            },
        ]
        if total_export_kwh > 0:
            sections.append({
                'title': 'Solar Export',
                'kwh': round(total_export_kwh, 2),
                'rate': self.FIT_RATE,
                'cost': round(-total_export_credit, 2),
            })

        supply_charge = self.daily_supply_charge * days
        total_energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(total_energy, 2),
            'supply_charge': round(supply_charge, 2),
            'total': round(total_energy + supply_charge, 2),
            'days': round(days, 1),
            'demand_charge_note': 'Peak demand charge (42.3c/kW/day) not included — adds ~$40-80/month depending on peak draw',
        }


class EnergyAustraliaEVPlan(RetailerPlan):
    """Energy Australia - EV Night Boost."""
    
    def __init__(self):
        super().__init__()
        self.retailer = "EnergyAustralia"
        self.plan_name = "EV Night Boost"
        self.daily_supply_charge = 1.10
        self.feed_in_tariff = 0.05
        
        self.off_peak_rate = 0.07  # 12am-6am
        self.peak_rate = 0.32
    
    def get_import_rate(self, dt: datetime) -> float:
        """Get rate based on time of day."""
        hour = dt.hour
        
        if 0 <= hour < 6:  # Off-peak 12am-6am
            return self.off_peak_rate
        else:  # Peak all other times
            return self.peak_rate
    
    def describe_strategy(self) -> str:
        """Describe battery strategy."""
        return (
            "Energy Australia's EV Night Boost strategy:\n"
            "• Charge battery overnight (12am-6am) at 7c/kWh\n"
            "• Discharge during day/evening to avoid 32c/kWh peak\n"
            "• Simple two-rate structure makes optimization easy\n"
            "• Best for people with consistent overnight charging access"
        )
    
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        """Get cost breakdown."""
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30
        
        offpeak_import = 0
        offpeak_cost = 0
        peak_import = 0
        peak_cost = 0
        total_export_kwh = 0
        total_export_credit = 0
        
        for hour_data in schedule:
            hour = hour_data.get('hour', 0) % 24
            import_kwh = hour_data.get('import_kwh', 0)
            import_cost = hour_data.get('import_cost', 0)
            export_kwh = hour_data.get('export_kwh', 0)
            export_credit = hour_data.get('export_credit', 0)
            
            total_export_kwh += export_kwh
            total_export_credit += export_credit
            
            if 0 <= hour < 6:
                offpeak_import += import_kwh
                offpeak_cost += import_cost
            else:
                peak_import += import_kwh
                peak_cost += import_cost
        
        sections = [
            {
                'title': 'Off-Peak (12am-6am)',
                'kwh': round(offpeak_import, 2),
                'rate': self.off_peak_rate,
                'cost': round(offpeak_cost, 2),
            },
            {
                'title': 'Peak (6am-12am)',
                'kwh': round(peak_import, 2),
                'rate': self.peak_rate,
                'cost': round(peak_cost, 2),
            },
        ]
        
        if total_export_kwh > 0:
            sections.append({
                'title': 'Solar Export',
                'kwh': round(total_export_kwh, 2),
                'rate': self.feed_in_tariff,
                'cost': round(-total_export_credit, 2),
            })
        
        supply_charge = self.daily_supply_charge * days
        total_energy = optimization_result.get('net_cost', 0)
        
        return {
            'sections': sections,
            'total_energy_cost': round(total_energy, 2),
            'supply_charge': round(supply_charge, 2),
            'total': round(total_energy + supply_charge, 2),
            'days': round(days, 1),
        }


class AGLNightSaverPlan(RetailerPlan):
    """AGL - Night Saver EV."""
    
    def __init__(self):
        super().__init__()
        self.retailer = "AGL"
        self.plan_name = "Night Saver EV"
        self.daily_supply_charge = 1.15
        self.feed_in_tariff = 0.05
        
        self.off_peak_rate = 0.08  # 10pm-7am
        self.peak_rate = 0.35
    
    def get_import_rate(self, dt: datetime) -> float:
        """Get rate based on time of day."""
        hour = dt.hour
        
        if 22 <= hour or hour < 7:  # 10pm-7am
            return self.off_peak_rate
        else:
            return self.peak_rate
    
    def describe_strategy(self) -> str:
        """Describe battery strategy."""
        return (
            "AGL Night Saver EV strategy:\n"
            "• Long off-peak window (10pm-7am) at 8c/kWh\n"
            "• Charge battery overnight\n"
            "• Discharge during expensive day hours (35c/kWh!)\n"
            "• 9-hour off-peak window gives flexibility"
        )
    
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        """Get cost breakdown."""
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30
        
        offpeak_import = 0
        offpeak_cost = 0
        peak_import = 0
        peak_cost = 0
        total_export_kwh = 0
        total_export_credit = 0
        
        for hour_data in schedule:
            hour = hour_data.get('hour', 0) % 24
            import_kwh = hour_data.get('import_kwh', 0)
            import_cost = hour_data.get('import_cost', 0)
            export_kwh = hour_data.get('export_kwh', 0)
            export_credit = hour_data.get('export_credit', 0)
            
            total_export_kwh += export_kwh
            total_export_credit += export_credit
            
            if 22 <= hour or hour < 7:
                offpeak_import += import_kwh
                offpeak_cost += import_cost
            else:
                peak_import += import_kwh
                peak_cost += import_cost
        
        sections = [
            {
                'title': 'Off-Peak (10pm-7am)',
                'kwh': round(offpeak_import, 2),
                'rate': self.off_peak_rate,
                'cost': round(offpeak_cost, 2),
            },
            {
                'title': 'Peak (7am-10pm)',
                'kwh': round(peak_import, 2),
                'rate': self.peak_rate,
                'cost': round(peak_cost, 2),
            },
        ]
        
        if total_export_kwh > 0:
            sections.append({
                'title': 'Solar Export',
                'kwh': round(total_export_kwh, 2),
                'rate': self.feed_in_tariff,
                'cost': round(-total_export_credit, 2),
            })
        
        supply_charge = self.daily_supply_charge * days
        total_energy = optimization_result.get('net_cost', 0)
        
        return {
            'sections': sections,
            'total_energy_cost': round(total_energy, 2),
            'supply_charge': round(supply_charge, 2),
            'total': round(total_energy + supply_charge, 2),
            'days': round(days, 1),
        }


class FlowPowerPlan(RetailerPlan):
    """Flow Power - Wholesale pricing with carbon offset."""

    # Peak FiT window: 17:30–19:30 at 45c/kWh. No FiT outside this window.
    # HA statistics are hourly; the window start (17:30) falls mid-hour, so the
    # 17:00 bucket captures 17:30–17:59 export and must be included.
    FIT_PEAK_RATE = 0.45

    # PEA (Price Efficiency Adjustment): computed from AEMO spot prices vs usage.
    # CPEA = LWAP - TWAP; PEA = CPEA - BPEA; credit = -PEA * total_kwh
    # BPEA (benchmark) ≈ 1.7c/kWh, adjusted annually by Flow Power.
    BPEA = 0.017  # $/kWh
    AEMO_PRICE_SENSOR = "sensor.aemo_nem_nsw1_current_5min_period_price"

    def __init__(self):
        super().__init__()
        self.retailer = "Flow Power"
        self.plan_name = "Flow Home"
        self.daily_supply_charge = 1.342
        self.feed_in_tariff = 0.0  # No FiT outside peak window
        self.is_market_linked = False  # Fixed tariff: 33.998c/kWh import, 45c FiT 17:30-19:30
        self.bpea = self.BPEA
        self.aemo_price_sensor = self.AEMO_PRICE_SENSOR

    def get_export_rate(self, dt: datetime) -> float:
        """45c/kWh for hours 17, 18, 19 (local); nothing outside.

        The FiT window is 17:30–19:30. HA hourly statistics use the hour-start
        as the bucket label, so all export that occurs from 17:30 onwards is
        stored in the 17:00 bucket. Crediting hours 17–19 captures the full
        window with hourly-granularity data.
        """
        if 17 <= dt.hour <= 19:
            return self.FIT_PEAK_RATE
        return 0.0

    def get_import_rate(self, dt: datetime) -> float:
        """Flow Power flat base rate — wholesale spot affects the PEA credit, not the per-kWh rate."""
        return 0.33998
    
    def describe_strategy(self) -> str:
        return (
            "Flow Power Flow Home strategy:\n"
            "• Fixed import rate 34.0c/kWh (NOT wholesale per-kWh — flat all day)\n"
            "• Key lever: 45c/kWh FiT window 17:30–19:30 (hrs 17–19 in model)\n"
            "• Outside that window: 0c FiT — exporting at other times earns nothing\n"
            "• Strategy: charge battery from solar, discharge 5:30–7:30pm for 45c credit\n"
            "• PEA (Price Efficiency Adjustment): post-hoc credit/charge based on AEMO\n"
            "  wholesale prices vs your consumption profile — reduces bill if you consume\n"
            "  when wholesale is cheap; adds to bill if you consume at expensive times\n"
            "• 100% carbon offset included in pricing\n"
            "• Best when battery exports align precisely with the 2-hour FiT window"
        )
    
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        """Get cost breakdown."""
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30
        
        peak_import = 0
        peak_cost = 0
        shoulder_import = 0
        shoulder_cost = 0
        offpeak_import = 0
        offpeak_cost = 0
        total_export_kwh = 0
        total_export_credit = 0
        
        for hour_data in schedule:
            hour = hour_data.get('hour', 0) % 24
            import_kwh = hour_data.get('import_kwh', 0)
            import_cost = hour_data.get('import_cost', 0)
            export_kwh = hour_data.get('export_kwh', 0)
            export_credit = hour_data.get('export_credit', 0)
            
            total_export_kwh += export_kwh
            total_export_credit += export_credit
            
            if 6 <= hour < 9 or 17 <= hour < 21:
                peak_import += import_kwh
                peak_cost += import_cost
            elif 10 <= hour < 16:
                shoulder_import += import_kwh
                shoulder_cost += import_cost
            else:
                offpeak_import += import_kwh
                offpeak_cost += import_cost
        
        sections = [
            {
                'title': 'Peak Wholesale (6-9am, 5-9pm)',
                'kwh': round(peak_import, 2),
                'rate': round(peak_cost / peak_import, 3) if peak_import > 0 else 0,
                'cost': round(peak_cost, 2),
            },
            {
                'title': 'Solar Hours (10am-4pm)',
                'kwh': round(shoulder_import, 2),
                'rate': round(shoulder_cost / shoulder_import, 3) if shoulder_import > 0 else 0,
                'cost': round(shoulder_cost, 2),
            },
            {
                'title': 'Off-Peak Wholesale',
                'kwh': round(offpeak_import, 2),
                'rate': round(offpeak_cost / offpeak_import, 3) if offpeak_import > 0 else 0,
                'cost': round(offpeak_cost, 2),
            },
        ]
        if total_export_kwh > 0:
            sections.append({
                'title': 'Solar Export (Credit)',
                'kwh': round(total_export_kwh, 2),
                'rate': round(total_export_credit / total_export_kwh, 3) if total_export_kwh > 0 else 0,
                'cost': round(-total_export_credit, 2),
            })
        
        supply_charge = self.daily_supply_charge * days
        total_energy = optimization_result.get('net_cost', 0)
        
        return {
            'sections': sections,
            'total_energy_cost': round(total_energy, 2),
            'supply_charge': round(supply_charge, 2),
            'total': round(total_energy + supply_charge, 2),
            'days': round(days, 1),
        }


class GloBirdZeroHeroPlan(RetailerPlan):
    """GloBird Energy – ZEROHERO.

    Rates sourced from WATTever, June 2026 (Ausgrid network).
    Key features:
      ZeroCharge: 0c/kWh import 11am-2pm (up to 50 kWh/day cap).
      Standard import: 59.40c/kWh at all other hours (flat, high).
      Super Export Credit: 10c/kWh for first 15 kWh exported 6pm-9pm daily.
      Evening FiT: 2c/kWh for exports 4pm-11pm outside super export window.
      No FiT overnight (11pm-4pm).
      ZeroHero $1/day credit if grid import < 0.03 kWh/hour during 6pm-9pm.
        Modelled as fixed_daily_credit (battery households will achieve this condition).
      VPP ZEROLIMITS: $1/kWh export during Critical Peak events (exogenous — not modelled).
    """

    STANDARD_IMPORT     = 0.5940  # $/kWh, all hours outside free window
    FREE_START          = 11      # 11am
    FREE_END            = 14      # up to 2pm (exclusive)
    FREE_CAP_KWH        = 50      # kWh/day (generous; rarely hit)
    SUPER_EXPORT_START  = 18      # 6pm
    SUPER_EXPORT_END    = 21      # 9pm
    SUPER_EXPORT_RATE   = 0.10    # $/kWh, first 15 kWh/day exported 6-9pm
    EVENING_EXPORT_START = 16     # 4pm
    EVENING_EXPORT_END   = 23     # 11pm
    EVENING_EXPORT_RATE  = 0.02   # $/kWh, 4pm-11pm outside super export window
    AVOID_PEAK_CREDIT   = 1.00    # $/day for zero 6pm-9pm grid import (<0.03 kWh/hr)

    def __init__(self):
        super().__init__()
        self.retailer = "GloBird"
        self.plan_name = "ZEROHERO"
        self.daily_supply_charge = 1.815   # $/day (NSW Ausgrid)
        self.feed_in_tariff = self.SUPER_EXPORT_RATE
        # $1/day credit when battery prevents any 6pm-9pm grid import.
        # Assumed achievable for battery households — modelled as fixed credit.
        self.fixed_daily_credit = self.AVOID_PEAK_CREDIT

    def get_import_rate(self, dt: datetime) -> float:
        h = dt.hour
        if self.FREE_START <= h < self.FREE_END:
            return 0.0   # ZeroCharge free window (50 kWh/day cap — rarely hit)
        return self.STANDARD_IMPORT

    def get_export_rate(self, dt: datetime) -> float:
        h = dt.hour
        if self.SUPER_EXPORT_START <= h < self.SUPER_EXPORT_END:
            return self.SUPER_EXPORT_RATE   # 10c (first 15 kWh/day — modelled flat)
        if self.EVENING_EXPORT_START <= h < self.EVENING_EXPORT_END:
            return self.EVENING_EXPORT_RATE  # 2c during 4pm-11pm shoulder
        return 0.0   # No FiT overnight or morning

    def describe_strategy(self) -> str:
        return (
            "GloBird ZEROHERO strategy:\n"
            "• FREE electricity 11am-2pm (up to 50 kWh/day) — charge battery fully\n"
            "• Standard rate 59.4c/kWh at ALL other hours — very high, avoid grid import\n"
            "• $1/day credit: keep grid import < 0.03 kWh/hr during 6pm-9pm (~$365/yr)\n"
            "• Super Export: 10c/kWh for first 15 kWh exported 6pm-9pm daily\n"
            "• Evening FiT: 2c/kWh (4pm-11pm outside super export window)\n"
            "• No FiT overnight (11pm-4pm) — don't export at other times\n"
            "• VPP ZEROLIMITS: $1/kWh bonus export during Critical Peak events (extra)\n"
            "• Optimal: charge battery free 11am-2pm → discharge 6pm-9pm\n"
            "  earns 10c FiT + $1 avoid-peak credit — double incentive for that window\n"
            "• High supply charge ($1.82/day) partly offset by $1/day avoid-peak credit"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        free_imp = standard_imp = 0.0
        free_cost = standard_cost = 0.0
        super_exp = evening_exp = other_exp = 0.0
        super_credit = evening_credit = 0.0

        for slot in schedule:
            h = slot.get('hour', 0) % 24
            imp = slot.get('import_kwh', 0.0)
            ic = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            ec = slot.get('export_credit', 0.0)
            if self.FREE_START <= h < self.FREE_END:
                free_imp += imp; free_cost += ic
            else:
                standard_imp += imp; standard_cost += ic
            if self.SUPER_EXPORT_START <= h < self.SUPER_EXPORT_END:
                super_exp += exp; super_credit += ec
            elif self.EVENING_EXPORT_START <= h < self.EVENING_EXPORT_END:
                evening_exp += exp; evening_credit += ec
            else:
                other_exp += exp

        sections = [
            {'title': 'ZeroCharge Free Window (11am-2pm, ≤50 kWh/day)',
             'kwh': round(free_imp, 2), 'rate': 0.0, 'cost': 0.0, 'highlight': True},
            {'title': 'Standard Import (all other hours)',
             'kwh': round(standard_imp, 2), 'rate': self.STANDARD_IMPORT,
             'cost': round(standard_cost, 2)},
        ]
        if super_exp > 0:
            sections.append({'title': 'Super Export 6-9pm (10c, first 15 kWh/day)',
                             'kwh': round(super_exp, 2), 'rate': self.SUPER_EXPORT_RATE,
                             'cost': round(-super_credit, 2)})
        if evening_exp > 0:
            sections.append({'title': 'Evening Export 4pm-11pm (2c)',
                             'kwh': round(evening_exp, 2), 'rate': self.EVENING_EXPORT_RATE,
                             'cost': round(-evening_credit, 2)})

        supply = self.daily_supply_charge * days
        avoid_credit = self.fixed_daily_credit * days
        energy = optimization_result.get('net_cost', 0.0)
        sections.append({'title': 'ZeroHero Avoid-Peak Credit ($1/day)',
                         'kwh': 0, 'rate': 0, 'cost': round(-avoid_credit, 2)})
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'avoid_peak_credit': round(avoid_credit, 2),
            'total': round(energy + supply - avoid_credit, 2),
            'days': round(days, 1),
            'note': (
                '$1/day credit assumes battery prevents 6pm-9pm grid draw. '
                'VPP ZEROLIMITS ($1/kWh Critical Peak export) not included — additional upside.'
            ),
        }


class GloBirdPlan(RetailerPlan):
    """GloBird - Time-of-use with solar focus."""

    def __init__(self):
        super().__init__()
        self.retailer = "GloBird"
        self.plan_name = "Solar Saver"
        self.daily_supply_charge = 0.95
        self.feed_in_tariff = 0.10  # Higher feed-in than most
        
        # GloBird rates (approximate)
        self.super_off_peak_rate = 0.08  # 10pm-6am
        self.shoulder_rate = 0.20  # 6am-4pm & 9pm-10pm
        self.peak_rate = 0.32  # 4pm-9pm
    
    def get_import_rate(self, dt: datetime) -> float:
        """Get rate based on GloBird's time-of-use structure."""
        hour = dt.hour
        
        if 22 <= hour or hour < 6:  # 10pm-6am super off-peak
            return self.super_off_peak_rate
        elif 16 <= hour < 21:  # 4pm-9pm peak
            return self.peak_rate
        else:  # 6am-4pm & 9pm-10pm shoulder
            return self.shoulder_rate
    
    def get_export_rate(self, dt: datetime) -> float:
        """GloBird offers higher feed-in tariff."""
        return self.feed_in_tariff
    
    def describe_strategy(self) -> str:
        """Describe battery strategy."""
        return (
            "GloBird Solar Saver strategy:\n"
            "• HIGH feed-in tariff (10c/kWh) - best for solar exports!\n"
            "• Super off-peak overnight (10pm-6am) at 8c/kWh\n"
            "• Avoid 4-9pm peak (32c/kWh)\n"
            "• Strategy: Export as much solar as possible (10c), charge battery overnight (8c)\n"
            "• Perfect for high solar production systems"
        )
    
    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        """Get cost breakdown highlighting high feed-in."""
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30
        
        super_offpeak_import = 0
        super_offpeak_cost = 0
        shoulder_import = 0
        shoulder_cost = 0
        peak_import = 0
        peak_cost = 0
        total_export_kwh = 0
        total_export_credit = 0
        
        for hour_data in schedule:
            hour = hour_data.get('hour', 0) % 24
            import_kwh = hour_data.get('import_kwh', 0)
            import_cost = hour_data.get('import_cost', 0)
            export_kwh = hour_data.get('export_kwh', 0)
            export_credit = hour_data.get('export_credit', 0)
            
            total_export_kwh += export_kwh
            total_export_credit += export_credit
            
            if 22 <= hour or hour < 6:
                super_offpeak_import += import_kwh
                super_offpeak_cost += import_cost
            elif 16 <= hour < 21:
                peak_import += import_kwh
                peak_cost += import_cost
            else:
                shoulder_import += import_kwh
                shoulder_cost += import_cost
        
        sections = [
            {
                'title': 'Super Off-Peak (10pm-6am)',
                'kwh': round(super_offpeak_import, 2),
                'rate': self.super_off_peak_rate,
                'cost': round(super_offpeak_cost, 2),
            },
            {
                'title': 'Shoulder (6am-4pm, 9-10pm)',
                'kwh': round(shoulder_import, 2),
                'rate': self.shoulder_rate,
                'cost': round(shoulder_cost, 2),
            },
            {
                'title': 'Peak (4-9pm)',
                'kwh': round(peak_import, 2),
                'rate': self.peak_rate,
                'cost': round(peak_cost, 2),
            },
        ]
        
        if total_export_kwh > 0:
            sections.append({
                'title': 'Solar Export (10c/kWh!) 🌞',
                'kwh': round(total_export_kwh, 2),
                'rate': self.feed_in_tariff,
                'cost': round(-total_export_credit, 2),
                'highlight': True,
            })
        
        supply_charge = self.daily_supply_charge * days
        total_energy = optimization_result.get('net_cost', 0)
        
        return {
            'sections': sections,
            'total_energy_cost': round(total_energy, 2),
            'supply_charge': round(supply_charge, 2),
            'total': round(total_energy + supply_charge, 2),
            'days': round(days, 1),
            'high_feedin_benefit': round(total_export_kwh * (self.feed_in_tariff - 0.05), 2),  # Extra vs standard 5c
        }


class OriginBatteryMaximiserPlan(RetailerPlan):
    """Origin Energy – Battery Maximiser (Origin Loop VPP).

    Rates sourced from WATTever / SolarQuotes June 2026 (Ausgrid network).
    Two-rate TOU: peak 5pm-9pm, off-peak all other hours.
    The high peak FiT (22c) is Origin Loop's VPP payment mechanism —
    battery exports during the peak window earn this rate automatically.
    No separate fixed VPP credit; the value is embedded in the export rate.
    """

    PEAK_IMPORT  = 0.539   # $/kWh, 5pm-9pm
    OFFPEAK_IMPORT = 0.187 # $/kWh, all other hours
    PEAK_FIT     = 0.22    # $/kWh, 5pm-9pm
    OFFPEAK_FIT  = 0.05    # $/kWh, all other hours

    def __init__(self):
        super().__init__()
        self.retailer = "Origin Energy"
        self.plan_name = "Battery Maximiser"
        self.daily_supply_charge = 1.2567
        self.feed_in_tariff = self.OFFPEAK_FIT

    def _is_peak(self, dt: datetime) -> bool:
        return 17 <= dt.hour < 21

    def get_import_rate(self, dt: datetime) -> float:
        return self.PEAK_IMPORT if self._is_peak(dt) else self.OFFPEAK_IMPORT

    def get_export_rate(self, dt: datetime) -> float:
        return self.PEAK_FIT if self._is_peak(dt) else self.OFFPEAK_FIT

    def describe_strategy(self) -> str:
        return (
            "Origin Battery Maximiser (Loop VPP) strategy:\n"
            "• 2-rate TOU: off-peak 18.7c (all hours outside 5-9pm) vs peak 53.9c (5-9pm)\n"
            "• PRIMARY incentive: discharging at peak avoids 53.9c import — 35c arbitrage\n"
            "  vs off-peak (18.7c); secondary: exporting earns 22c FiT during peak window\n"
            "• Charge battery overnight or from solar at off-peak rate (18.7c)\n"
            "• Hold full battery charge for 5-9pm peak — discharge aggressively then\n"
            "• Origin auto-dispatches battery during grid events — reserve ≥20%\n"
            "• 200 kWh/year VPP dispatch cap; no additional fixed credit\n"
            "• Best for households that can tolerate automation of battery"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        peak_import = offpeak_import = 0.0
        peak_cost = offpeak_cost = 0.0
        peak_export = offpeak_export = 0.0
        peak_credit = offpeak_credit = 0.0

        for slot in schedule:
            h = slot.get('hour', 0) % 24
            imp = slot.get('import_kwh', 0.0)
            imp_cost = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            exp_credit = slot.get('export_credit', 0.0)
            if 17 <= h < 21:
                peak_import += imp; peak_cost += imp_cost
                peak_export += exp; peak_credit += exp_credit
            else:
                offpeak_import += imp; offpeak_cost += imp_cost
                offpeak_export += exp; offpeak_credit += exp_credit

        sections = [
            {'title': 'Peak Import (5-9pm)', 'kwh': round(peak_import, 2),
             'rate': self.PEAK_IMPORT, 'cost': round(peak_cost, 2)},
            {'title': 'Off-Peak Import', 'kwh': round(offpeak_import, 2),
             'rate': self.OFFPEAK_IMPORT, 'cost': round(offpeak_cost, 2)},
        ]
        total_export = peak_export + offpeak_export
        total_credit = peak_credit + offpeak_credit
        if total_export > 0:
            blended = total_credit / total_export if total_export > 0 else 0
            sections.append({'title': 'Solar/Battery Export (22c peak, 5c other)',
                             'kwh': round(total_export, 2), 'rate': round(blended, 3),
                             'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'total': round(energy + supply, 2),
            'days': round(days, 1),
        }


class EnergyAustraliaBatteryEasePlan(RetailerPlan):
    """EnergyAustralia – BatteryEase (NSW-only VPP).

    Rates sourced from SolarQuotes big-three comparison, June 2026.
    Standard Ausgrid TOU time windows applied (peak Mon-Fri 2pm-8pm).
    FiT capped at 12c for first 15 kWh/day; 7.6c beyond.
    For hourly modelling we use 12c flat (conservative; assumes <15 kWh/day export).
    Fixed VPP credit: $15/month ($180/year) guaranteed regardless of dispatch frequency.
    """

    PEAK_IMPORT     = 0.6423  # Mon-Fri 2pm-8pm
    SHOULDER_IMPORT = 0.3604  # Mon-Fri 7am-2pm & 8pm-10pm, weekends 7am-10pm
    OFFPEAK_IMPORT  = 0.2769  # 10pm-7am all days
    FIT_RATE        = 0.12    # $/kWh (first 15 kWh/day; 7.6c beyond — modelled flat)
    MONTHLY_CREDIT  = 15.0    # $ per month VPP participation credit

    def __init__(self):
        super().__init__()
        self.retailer = "EnergyAustralia"
        self.plan_name = "BatteryEase"
        self.daily_supply_charge = 1.0549
        self.feed_in_tariff = self.FIT_RATE
        self.fixed_daily_credit = self.MONTHLY_CREDIT / 30.44  # ~$0.493/day

    def _rate_period(self, dt: datetime) -> str:
        h = dt.hour
        is_weekday = dt.weekday() < 5
        if 22 <= h or h < 7:
            return 'offpeak'
        if is_weekday and 14 <= h < 20:
            return 'peak'
        return 'shoulder'

    def get_import_rate(self, dt: datetime) -> float:
        p = self._rate_period(dt)
        return {'peak': self.PEAK_IMPORT, 'shoulder': self.SHOULDER_IMPORT,
                'offpeak': self.OFFPEAK_IMPORT}[p]

    def describe_strategy(self) -> str:
        return (
            "EnergyAustralia BatteryEase (NSW VPP) strategy:\n"
            "• 3-rate TOU: peak 64.2c (Mon-Fri 2-8pm), shoulder 36c, off-peak 27.7c\n"
            "• Discharge battery during peak to avoid 64c/kWh — largest spread in NSW\n"
            "• Export earns 12c/kWh (first 15 kWh/day) — good FiT\n"
            "• Guaranteed $15/month VPP credit regardless of how often EA accesses battery\n"
            "• EA dispatches up to 200 kWh/year; retains ≥10% battery reserve\n"
            "• NSW only; rates changing July 2026 — verify before switching"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        peak_imp = shoulder_imp = offpeak_imp = 0.0
        peak_cost = shoulder_cost = offpeak_cost = 0.0
        total_exp = total_credit = 0.0

        for slot in schedule:
            dt_proxy = datetime(2026, 6, 16, slot.get('hour', 0) % 24)  # Monday proxy
            p = self._rate_period(dt_proxy)
            imp = slot.get('import_kwh', 0.0)
            ic = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            ec = slot.get('export_credit', 0.0)
            if p == 'peak':
                peak_imp += imp; peak_cost += ic
            elif p == 'shoulder':
                shoulder_imp += imp; shoulder_cost += ic
            else:
                offpeak_imp += imp; offpeak_cost += ic
            total_exp += exp; total_credit += ec

        sections = [
            {'title': 'Peak (Mon-Fri 2-8pm)', 'kwh': round(peak_imp, 2),
             'rate': self.PEAK_IMPORT, 'cost': round(peak_cost, 2)},
            {'title': 'Shoulder', 'kwh': round(shoulder_imp, 2),
             'rate': self.SHOULDER_IMPORT, 'cost': round(shoulder_cost, 2)},
            {'title': 'Off-Peak (10pm-7am)', 'kwh': round(offpeak_imp, 2),
             'rate': self.OFFPEAK_IMPORT, 'cost': round(offpeak_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export (12c, first 15 kWh/day)',
                             'kwh': round(total_exp, 2), 'rate': self.FIT_RATE,
                             'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        vpp_credit = self.fixed_daily_credit * days
        energy = optimization_result.get('net_cost', 0.0)
        subtotal = energy + supply
        sections.append({'title': 'VPP Credit ($15/month)', 'kwh': 0,
                         'rate': 0, 'cost': round(-vpp_credit, 2)})
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'vpp_credit': round(vpp_credit, 2),
            'total': round(subtotal - vpp_credit, 2),
            'days': round(days, 1),
        }


class AGLBatteryRewardsPlan(RetailerPlan):
    """AGL – Battery Rewards Plan (VPP, distinct from Night Saver EV).

    Rates sourced from SolarQuotes big-three comparison, June 2026 (Ausgrid).
    Two-rate TOU: peak 5pm-9pm, off-peak all other hours.
    Peak export earns 25c/kWh (paid as Everyday Rewards gift cards).
    Fixed VPP credit: $80/year ($20/quarter guaranteed quarterly bill credit).
    """

    PEAK_IMPORT    = 0.54527  # 5pm-9pm all days
    OFFPEAK_IMPORT = 0.19998  # all other hours
    PEAK_FIT       = 0.25     # 5pm-9pm (gift card / bill credit)
    STANDARD_FIT   = 0.04     # all other hours
    ANNUAL_CREDIT  = 80.0     # $ guaranteed per year in quarterly credits

    def __init__(self):
        super().__init__()
        self.retailer = "AGL"
        self.plan_name = "Battery Rewards"
        self.daily_supply_charge = 1.30691
        self.feed_in_tariff = self.STANDARD_FIT
        self.fixed_daily_credit = self.ANNUAL_CREDIT / 365.25

    def _is_peak(self, dt: datetime) -> bool:
        return 17 <= dt.hour < 21

    def get_import_rate(self, dt: datetime) -> float:
        return self.PEAK_IMPORT if self._is_peak(dt) else self.OFFPEAK_IMPORT

    def get_export_rate(self, dt: datetime) -> float:
        return self.PEAK_FIT if self._is_peak(dt) else self.STANDARD_FIT

    def describe_strategy(self) -> str:
        return (
            "AGL Battery Rewards strategy:\n"
            "• 2-rate TOU: off-peak 20c all day, expensive peak 54.5c at 5-9pm\n"
            "• Discharge battery during peak to avoid 54.5c/kWh\n"
            "• Peak export earns 25c/kWh (paid via Everyday Rewards gift cards)\n"
            "• Guaranteed $80/year in quarterly bill credits ($20/quarter)\n"
            "• No cap on battery exports — you control the timing\n"
            "• Combine with overnight charging (20c) for strong arbitrage spread"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        peak_imp = offpeak_imp = 0.0
        peak_cost = offpeak_cost = 0.0
        peak_exp = offpeak_exp = 0.0
        peak_credit = offpeak_credit = 0.0

        for slot in schedule:
            h = slot.get('hour', 0) % 24
            imp = slot.get('import_kwh', 0.0)
            ic = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            ec = slot.get('export_credit', 0.0)
            if 17 <= h < 21:
                peak_imp += imp; peak_cost += ic
                peak_exp += exp; peak_credit += ec
            else:
                offpeak_imp += imp; offpeak_cost += ic
                offpeak_exp += exp; offpeak_credit += ec

        sections = [
            {'title': 'Peak Import (5-9pm)', 'kwh': round(peak_imp, 2),
             'rate': self.PEAK_IMPORT, 'cost': round(peak_cost, 2)},
            {'title': 'Off-Peak Import', 'kwh': round(offpeak_imp, 2),
             'rate': self.OFFPEAK_IMPORT, 'cost': round(offpeak_cost, 2)},
        ]
        total_exp = peak_exp + offpeak_exp
        total_credit = peak_credit + offpeak_credit
        if total_exp > 0:
            blended = total_credit / total_exp if total_exp > 0 else 0
            sections.append({'title': 'Export (25c peak, 4c other)',
                             'kwh': round(total_exp, 2), 'rate': round(blended, 3),
                             'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        vpp_credit = self.fixed_daily_credit * days
        energy = optimization_result.get('net_cost', 0.0)
        sections.append({'title': 'Quarterly VPP Credit ($80/yr)', 'kwh': 0,
                         'rate': 0, 'cost': round(-vpp_credit, 2)})
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'vpp_credit': round(vpp_credit, 2),
            'total': round(energy + supply - vpp_credit, 2),
            'days': round(days, 1),
        }


class ENGIEVPPAdvantagePlan(RetailerPlan):
    """ENGIE – VPP Advantage (Anytime flat rate).

    Rates sourced from WATTever battery/VPP comparison page, June 2026 (Ausgrid).
    Notable feature: very low daily supply charge (43.9c/day) offset by higher usage rate.
    Annual VPP credit: ~$240/year. Sign-up bonus: $200 (excluded from ongoing comparison).
    """

    USAGE_RATE    = 0.4011  # $/kWh flat (anytime)
    FIT_RATE      = 0.08    # $/kWh
    ANNUAL_CREDIT = 240.0   # $ estimated annual VPP credit

    def __init__(self):
        super().__init__()
        self.retailer = "ENGIE"
        self.plan_name = "VPP Advantage"
        self.daily_supply_charge = 0.439
        self.feed_in_tariff = self.FIT_RATE
        self.fixed_daily_credit = self.ANNUAL_CREDIT / 365.25

    def get_import_rate(self, dt: datetime) -> float:
        return self.USAGE_RATE

    def describe_strategy(self) -> str:
        return (
            "ENGIE VPP Advantage strategy:\n"
            "• Flat 40.1c/kWh — no TOU complexity, but high usage rate\n"
            "• Very low supply charge (44c/day vs ~$1.30 for others)\n"
            "• Attractive for low-import households with large solar+battery\n"
            "• Good 8c FiT — above industry average\n"
            "• ~$240/year VPP credit; $200 sign-up bonus (one-off, excluded here)\n"
            "• Best when battery keeps grid import very low"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        total_imp = total_cost = total_exp = total_credit = 0.0
        for slot in schedule:
            total_imp += slot.get('import_kwh', 0.0)
            total_cost += slot.get('import_cost', 0.0)
            total_exp += slot.get('export_kwh', 0.0)
            total_credit += slot.get('export_credit', 0.0)

        sections = [
            {'title': 'Grid Import (flat rate)', 'kwh': round(total_imp, 2),
             'rate': self.USAGE_RATE, 'cost': round(total_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export', 'kwh': round(total_exp, 2),
                             'rate': self.FIT_RATE, 'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        vpp_credit = self.fixed_daily_credit * days
        energy = optimization_result.get('net_cost', 0.0)
        sections.append({'title': 'VPP Credit (~$240/yr)', 'kwh': 0,
                         'rate': 0, 'cost': round(-vpp_credit, 2)})
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'vpp_credit': round(vpp_credit, 2),
            'total': round(energy + supply - vpp_credit, 2),
            'days': round(days, 1),
        }


class AlintaSolarBalancePlan(RetailerPlan):
    """Alinta Energy – SolarBalance Go.

    Rates sourced from WATTever / Selectra, June 2026 (Ausgrid network).
    Flat usage rate. Stepped FiT: 10c/kWh for first 10 kWh exported per day,
    5c/kWh beyond. Modelled at 10c flat (conservative; assumes <10 kWh/day export).
    Requires solar inverter ≤10 kW.
    """

    USAGE_RATE    = 0.3435  # $/kWh flat
    FIT_TIER1     = 0.10    # $/kWh, first 10 kWh/day
    FIT_TIER2     = 0.05    # $/kWh, beyond 10 kWh/day

    def __init__(self):
        super().__init__()
        self.retailer = "Alinta Energy"
        self.plan_name = "SolarBalance Go"
        self.daily_supply_charge = 0.8708
        self.feed_in_tariff = self.FIT_TIER1  # Flat model; see note above

    def get_import_rate(self, dt: datetime) -> float:
        return self.USAGE_RATE

    def describe_strategy(self) -> str:
        return (
            "Alinta SolarBalance Go strategy:\n"
            "• Flat 34.4c/kWh — simple, no TOU optimisation needed\n"
            "• Stepped FiT: 10c/kWh for first 10 kWh/day exported, then 5c beyond\n"
            "• Solar export beyond 10 kWh/day earns only 5c — diminishing returns\n"
            "• Low supply charge (87c/day) — good for low-consuming households\n"
            "• Requires solar inverter ≤10 kW\n"
            "• With battery: charge cheaply, export solar during day for 10c FiT"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        total_imp = total_cost = total_exp = total_credit = 0.0
        for slot in schedule:
            total_imp += slot.get('import_kwh', 0.0)
            total_cost += slot.get('import_cost', 0.0)
            total_exp += slot.get('export_kwh', 0.0)
            total_credit += slot.get('export_credit', 0.0)

        sections = [
            {'title': 'Grid Import (flat rate)', 'kwh': round(total_imp, 2),
             'rate': self.USAGE_RATE, 'cost': round(total_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export (10c, first 10 kWh/day)',
                             'kwh': round(total_exp, 2), 'rate': self.FIT_TIER1,
                             'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'total': round(energy + supply, 2),
            'days': round(days, 1),
        }


class AGLSolarSharerPlan(RetailerPlan):
    """AGL – Solar Sharer Offer (government-mandated, NSW, effective 1 July 2026).

    Rates sourced from AGL plan documents / Whirlpool forums, June 2026 (Ausgrid).
    Mandatory offer from AGL, Origin, and EnergyAustralia for smart meter customers.
    Key feature: free electricity 11am-2pm (up to 24 kWh/day cap; rarely hit).
    No solar required to qualify. No FiT offered on this plan.
    Peak window (3pm-9pm) may vary seasonally; modelled flat here.
    """

    PEAK_IMPORT    = 0.6372   # $/kWh, 3pm-9pm (incl. GST)
    OFFPEAK_IMPORT = 0.2756   # $/kWh, all other hours
    FREE_START     = 11       # 11am
    FREE_END       = 14       # up to 2pm (exclusive)
    PEAK_START     = 15       # 3pm
    PEAK_END       = 21       # 9pm

    def __init__(self):
        super().__init__()
        self.retailer = "AGL"
        self.plan_name = "Solar Sharer"
        self.daily_supply_charge = 1.7624  # $/day (incl. GST)
        self.feed_in_tariff = 0.0          # No FiT on this plan

    def get_import_rate(self, dt: datetime) -> float:
        h = dt.hour
        if self.FREE_START <= h < self.FREE_END:
            return 0.0   # Free window (24 kWh/day cap — rarely hit in practice)
        if self.PEAK_START <= h < self.PEAK_END:
            return self.PEAK_IMPORT
        return self.OFFPEAK_IMPORT

    def describe_strategy(self) -> str:
        return (
            "AGL Solar Sharer strategy (effective 1 July 2026):\n"
            "• FREE electricity 11am-2pm (up to 24 kWh/day) — no solar required\n"
            "• Peak 63.7c/kWh (3pm-9pm) — extremely high; avoid at all costs\n"
            "• Off-peak 27.6c/kWh (all other hours including overnight)\n"
            "• Strategy: charge battery from FREE grid power 11am-2pm\n"
            "• Discharge battery 3pm-9pm to avoid 63.7c peak rate\n"
            "• No FiT — solar export earns nothing; maximise self-consumption\n"
            "• NOTE: solar households may earn less than on FiT plans;\n"
            "  benefit comes from free charging window + peak avoidance\n"
            "• High supply charge ($1.76/day) vs typical $1.05-1.30/day"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        free_imp = peak_imp = offpeak_imp = 0.0
        free_cost = peak_cost = offpeak_cost = 0.0
        total_exp = total_credit = 0.0

        for slot in schedule:
            h = slot.get('hour', 0) % 24
            imp = slot.get('import_kwh', 0.0)
            ic = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            ec = slot.get('export_credit', 0.0)
            if self.FREE_START <= h < self.FREE_END:
                free_imp += imp; free_cost += ic
            elif self.PEAK_START <= h < self.PEAK_END:
                peak_imp += imp; peak_cost += ic
            else:
                offpeak_imp += imp; offpeak_cost += ic
            total_exp += exp; total_credit += ec

        sections = [
            {'title': 'Free Window (11am-2pm, ≤24 kWh/day)', 'kwh': round(free_imp, 2),
             'rate': 0.0, 'cost': 0.0, 'highlight': True},
            {'title': 'Peak (3pm-9pm)', 'kwh': round(peak_imp, 2),
             'rate': self.PEAK_IMPORT, 'cost': round(peak_cost, 2)},
            {'title': 'Off-Peak (all other hours)', 'kwh': round(offpeak_imp, 2),
             'rate': self.OFFPEAK_IMPORT, 'cost': round(offpeak_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export (no FiT — zero credit)',
                             'kwh': round(total_exp, 2), 'rate': 0.0, 'cost': 0.0})

        supply = self.daily_supply_charge * days
        energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'total': round(energy + supply, 2),
            'days': round(days, 1),
            'note': 'AGL Solar Sharer effective 1 July 2026. Peak hours may vary seasonally.',
        }


class OriginSolarBoostPlan(RetailerPlan):
    """Origin Energy – Solar Boost.

    Rates sourced from WATTever, June 2026 (Ausgrid network).
    Flat usage rate (no TOU). Stepped FiT: 8c/kWh for first 8 kWh exported per day
    (averaged over billing period); 3c/kWh beyond cap.
    Modelled at 8c flat (conservative; assumes average daily export ≤8 kWh).
    """

    USAGE_RATE = 0.4049  # $/kWh flat
    FIT_TIER1  = 0.08    # $/kWh, first 8 kWh/day (averaged over billing period)
    FIT_TIER2  = 0.03    # $/kWh, beyond 8 kWh/day

    def __init__(self):
        super().__init__()
        self.retailer = "Origin Energy"
        self.plan_name = "Solar Boost"
        self.daily_supply_charge = 1.0561
        self.feed_in_tariff = self.FIT_TIER1  # Flat model; see note above

    def get_import_rate(self, dt: datetime) -> float:
        return self.USAGE_RATE

    def describe_strategy(self) -> str:
        return (
            "Origin Solar Boost strategy:\n"
            "• Flat 40.5c/kWh — no TOU, straightforward billing\n"
            "• Stepped FiT: 8c/kWh for first 8 kWh/day exported (averaged), 3c beyond\n"
            "• Low supply charge ($1.06/day) — competitive vs most plans\n"
            "• Strategy: maximise solar export up to ~8 kWh/day average for 8c credit\n"
            "• Beyond 8 kWh/day export: only earns 3c — diminishing returns\n"
            "• Battery: self-consume to reduce flat 40.5c import; surplus solar → export\n"
            "• Best for moderate-solar households exporting 5-10 kWh/day average"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        total_imp = total_cost = total_exp = total_credit = 0.0
        for slot in schedule:
            total_imp += slot.get('import_kwh', 0.0)
            total_cost += slot.get('import_cost', 0.0)
            total_exp += slot.get('export_kwh', 0.0)
            total_credit += slot.get('export_credit', 0.0)

        sections = [
            {'title': 'Grid Import (flat rate)', 'kwh': round(total_imp, 2),
             'rate': self.USAGE_RATE, 'cost': round(total_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export (8c, first 8 kWh/day avg; 3c beyond)',
                             'kwh': round(total_exp, 2), 'rate': self.FIT_TIER1,
                             'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'total': round(energy + supply, 2),
            'days': round(days, 1),
            'note': 'FiT modelled at 8c flat; actual rate drops to 3c beyond 8 kWh/day average.',
        }


class ENGIESolarEnergyPlan(RetailerPlan):
    """ENGIE – Solar Energy Plan (standard solar, non-VPP).

    Rates sourced from WATTever, June 2026 (Ausgrid network).
    Flat usage rate. FiT 8c/kWh capped at first 8 kWh exported per day.
    Distinct from ENGIEVPPAdvantagePlan: no battery/VPP credits here.
    $100 sign-up credit is one-off and excluded from ongoing cost comparison.
    """

    USAGE_RATE = 0.4011  # $/kWh flat
    FIT_RATE   = 0.08    # $/kWh (first 8 kWh/day)

    def __init__(self):
        super().__init__()
        self.retailer = "ENGIE"
        self.plan_name = "Solar Energy Plan"
        self.daily_supply_charge = 1.0965
        self.feed_in_tariff = self.FIT_RATE

    def get_import_rate(self, dt: datetime) -> float:
        return self.USAGE_RATE

    def describe_strategy(self) -> str:
        return (
            "ENGIE Solar Energy Plan strategy:\n"
            "• Flat 40.1c/kWh — no TOU, simple billing\n"
            "• Good FiT: 8c/kWh (capped at 8 kWh/day) — no VPP required\n"
            "• $100 sign-up credit (one-off, not in ongoing comparison)\n"
            "• Strategy: export solar up to 8 kWh/day for 8c credit\n"
            "• Battery: self-consume to reduce flat 40c import; export surplus for 8c\n"
            "• No battery compatibility requirements (unlike VPP Advantage)\n"
            "• Compare vs ENGIE VPP Advantage: VPP Advantage adds ~$240/yr credit\n"
            "  but requires compatible battery; this plan works with any or no battery"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        total_imp = total_cost = total_exp = total_credit = 0.0
        for slot in schedule:
            total_imp += slot.get('import_kwh', 0.0)
            total_cost += slot.get('import_cost', 0.0)
            total_exp += slot.get('export_kwh', 0.0)
            total_credit += slot.get('export_credit', 0.0)

        sections = [
            {'title': 'Grid Import (flat rate)', 'kwh': round(total_imp, 2),
             'rate': self.USAGE_RATE, 'cost': round(total_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export (8c, first 8 kWh/day)',
                             'kwh': round(total_exp, 2), 'rate': self.FIT_RATE,
                             'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'total': round(energy + supply, 2),
            'days': round(days, 1),
            'note': 'FiT capped at 8 kWh/day; $100 sign-up credit excluded.',
        }


class RedEnergyLivingEnergySaverPlan(RetailerPlan):
    """Red Energy – Living Energy Saver (TOU, Ausgrid network).

    Rates sourced from WATTever, June 2026 (Ausgrid EA025 TOU tariff).
    Standard Ausgrid TOU windows: peak Mon-Fri 2pm-8pm, off-peak 10pm-7am,
    shoulder all other times.
    FiT: 4c/kWh flat (Living Energy Saver; no solar-specific NSW plan available).
    """

    PEAK_IMPORT     = 0.4824  # Mon-Fri 2pm-8pm
    SHOULDER_IMPORT = 0.325   # weekdays 7am-2pm & 8pm-10pm, weekends 7am-10pm
    OFFPEAK_IMPORT  = 0.2418  # 10pm-7am all days
    FIT_RATE        = 0.04    # $/kWh flat

    def __init__(self):
        super().__init__()
        self.retailer = "Red Energy"
        self.plan_name = "Living Energy Saver"
        self.daily_supply_charge = 1.0092
        self.feed_in_tariff = self.FIT_RATE

    def _rate_period(self, dt: datetime) -> str:
        h = dt.hour
        is_weekday = dt.weekday() < 5
        if 22 <= h or h < 7:
            return 'offpeak'
        if is_weekday and 14 <= h < 20:
            return 'peak'
        return 'shoulder'

    def get_import_rate(self, dt: datetime) -> float:
        p = self._rate_period(dt)
        return {'peak': self.PEAK_IMPORT, 'shoulder': self.SHOULDER_IMPORT,
                'offpeak': self.OFFPEAK_IMPORT}[p]

    def describe_strategy(self) -> str:
        return (
            "Red Energy Living Energy Saver strategy:\n"
            "• 3-rate TOU: peak 48.2c (Mon-Fri 2-8pm), shoulder 32.5c, off-peak 24.2c\n"
            "• Discharge battery during weekday peak to avoid 48c/kWh\n"
            "• Recharge overnight (24c off-peak) or from solar\n"
            "• FiT only 4c/kWh — self-consumption and arbitrage matter more than export\n"
            "• Good for Snowy Hydro renewable credentials (Red Energy = Snowy Hydro)\n"
            "• No battery or VPP plan — simpler to manage than VPP alternatives"
        )

    def get_display_breakdown(self, optimization_result: Dict) -> Dict:
        schedule = optimization_result.get('schedule', [])
        days = len(schedule) / 24 if schedule else 30

        peak_imp = shoulder_imp = offpeak_imp = 0.0
        peak_cost = shoulder_cost = offpeak_cost = 0.0
        total_exp = total_credit = 0.0

        for slot in schedule:
            dt_proxy = datetime(2026, 6, 16, slot.get('hour', 0) % 24)  # weekday proxy
            p = self._rate_period(dt_proxy)
            imp = slot.get('import_kwh', 0.0)
            ic = slot.get('import_cost', 0.0)
            exp = slot.get('export_kwh', 0.0)
            ec = slot.get('export_credit', 0.0)
            if p == 'peak':
                peak_imp += imp; peak_cost += ic
            elif p == 'shoulder':
                shoulder_imp += imp; shoulder_cost += ic
            else:
                offpeak_imp += imp; offpeak_cost += ic
            total_exp += exp; total_credit += ec

        sections = [
            {'title': 'Peak (Mon-Fri 2-8pm)', 'kwh': round(peak_imp, 2),
             'rate': self.PEAK_IMPORT, 'cost': round(peak_cost, 2)},
            {'title': 'Shoulder', 'kwh': round(shoulder_imp, 2),
             'rate': self.SHOULDER_IMPORT, 'cost': round(shoulder_cost, 2)},
            {'title': 'Off-Peak (10pm-7am)', 'kwh': round(offpeak_imp, 2),
             'rate': self.OFFPEAK_IMPORT, 'cost': round(offpeak_cost, 2)},
        ]
        if total_exp > 0:
            sections.append({'title': 'Solar Export', 'kwh': round(total_exp, 2),
                             'rate': self.FIT_RATE, 'cost': round(-total_credit, 2)})

        supply = self.daily_supply_charge * days
        energy = optimization_result.get('net_cost', 0.0)
        return {
            'sections': sections,
            'total_energy_cost': round(energy, 2),
            'supply_charge': round(supply, 2),
            'total': round(energy + supply, 2),
            'days': round(days, 1),
        }


# Factory function to create plan instances
def create_plan(retailer: str, plan_name: str) -> Optional[RetailerPlan]:
    """Create a plan instance by retailer and plan name.
    
    Args:
        retailer: Retailer name
        plan_name: Plan name
        
    Returns:
        RetailerPlan instance or None if not found
    """
    plans = {
        ('Amber Electric', 'SmartShift'): AmberPlan,
        ('OVO Energy', 'The EV Plan'): OVOEVPlan,
        ('EnergyAustralia', 'EV Night Boost'): EnergyAustraliaEVPlan,
        ('EnergyAustralia', 'BatteryEase'): EnergyAustraliaBatteryEasePlan,
        ('AGL', 'Night Saver EV'): AGLNightSaverPlan,
        ('AGL', 'Battery Rewards'): AGLBatteryRewardsPlan,
        ('AGL', 'Solar Sharer'): AGLSolarSharerPlan,
        ('Flow Power', 'Flow Home'): FlowPowerPlan,
        ('GloBird', 'ZEROHERO'): GloBirdZeroHeroPlan,
        ('GloBird', 'Solar Saver'): GloBirdPlan,
        ('Origin Energy', 'Battery Maximiser'): OriginBatteryMaximiserPlan,
        ('Origin Energy', 'Solar Boost'): OriginSolarBoostPlan,
        ('ENGIE', 'VPP Advantage'): ENGIEVPPAdvantagePlan,
        ('ENGIE', 'Solar Energy Plan'): ENGIESolarEnergyPlan,
        ('Alinta Energy', 'SolarBalance Go'): AlintaSolarBalancePlan,
        ('Red Energy', 'Living Energy Saver'): RedEnergyLivingEnergySaverPlan,
    }
    
    plan_class = plans.get((retailer, plan_name))
    if plan_class:
        return plan_class()
    
    _LOGGER.warning(f"Unknown plan: {retailer} - {plan_name}")
    return None


def get_all_plans() -> List[RetailerPlan]:
    """Get all available plan instances.
    
    Returns:
        List of all plan instances
    """
    return [
        AmberPlan(),
        OVOEVPlan(),
        EnergyAustraliaEVPlan(),
        EnergyAustraliaBatteryEasePlan(),
        AGLNightSaverPlan(),
        AGLBatteryRewardsPlan(),
        AGLSolarSharerPlan(),
        FlowPowerPlan(),
        GloBirdZeroHeroPlan(),
        GloBirdPlan(),
        OriginBatteryMaximiserPlan(),
        OriginSolarBoostPlan(),
        ENGIEVPPAdvantagePlan(),
        ENGIESolarEnergyPlan(),
        AlintaSolarBalancePlan(),
        RedEnergyLivingEnergySaverPlan(),
    ]
