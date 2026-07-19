import React, { useState, useEffect } from 'react';
import { BarChart, Bar, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts';

export default function ElectricityDashboard() {
  const [dateRange, setDateRange] = useState('30');
  const [selectedPlan, setSelectedPlan] = useState(null);
  const [usageData, setUsageData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [sensorConfig, setSensorConfig] = useState(null);

  // Fetch sensor configuration from Home Assistant
  useEffect(() => {
    fetchSensorConfig();
  }, []);

  // Fetch data when date range changes
  useEffect(() => {
    if (sensorConfig) {
      fetchHomeAssistantData();
    }
  }, [dateRange, sensorConfig]);

  const fetchSensorConfig = async () => {
    try {
      // Get the integration config which includes sensor mappings
      const response = await fetch('/api/states/sensor.grid_lens_current_plan_monthly_cost');
      
      if (!response.ok) {
        throw new Error('Integration not configured');
      }
      
      const data = await response.json();
      
      // Extract sensor IDs from integration attributes
      const config = {
        energySensor: data.attributes.energy_sensor || 'sensor.energy_consumption',
        solarSensor: data.attributes.solar_sensor || null,
        exportSensor: data.attributes.export_sensor || null,
        purchasePriceSensor: data.attributes.purchase_price_sensor || 'sensor.amber_general_price',
        feedinPriceSensor: data.attributes.feedin_price_sensor || 'sensor.amber_feed_in_price',
      };
      
      setSensorConfig(config);
    } catch (err) {
      console.error('Failed to fetch sensor config:', err);
      // Use defaults if can't fetch
      setSensorConfig({
        energySensor: 'sensor.energy_consumption',
        solarSensor: null,
        exportSensor: null,
        purchasePriceSensor: 'sensor.amber_general_price',
        feedinPriceSensor: 'sensor.amber_feed_in_price',
      });
    }
  };

  const fetchHomeAssistantData = async () => {
    setLoading(true);
    setError(null);
    
    try {
      const endDate = new Date();
      const startDate = new Date();
      startDate.setDate(endDate.getDate() - parseInt(dateRange));

      // Build sensor list from config
      const sensorIds = [
        sensorConfig.energySensor,
        sensorConfig.solarSensor,
        sensorConfig.exportSensor,
        sensorConfig.purchasePriceSensor,
        sensorConfig.feedinPriceSensor,
      ].filter(Boolean); // Remove nulls

      const historyPromises = sensorIds.map(async (sensorId) => {
        try {
          const response = await fetch(
            `/api/history/period/${startDate.toISOString()}?filter_entity_id=${sensorId}&end_time=${endDate.toISOString()}`
          );
          
          if (!response.ok) {
            console.warn(`Failed to fetch ${sensorId}:`, response.status);
            return { sensorId, data: [] };
          }
          
          const data = await response.json();
          return { sensorId, data: data[0] || [] };
        } catch (err) {
          console.warn(`Error fetching ${sensorId}:`, err);
          return { sensorId, data: [] };
        }
      });

      const results = await Promise.all(historyPromises);
      const processedData = processHistoricalData(results, parseInt(dateRange));
      setUsageData(processedData);
      setLoading(false);
      
    } catch (err) {
      console.error('Error fetching Home Assistant data:', err);
      setError('Failed to load data from Home Assistant. Using simulated data instead.');
      setUsageData(generateMockData(dateRange));
      setLoading(false);
    }
  };

  const processHistoricalData = (sensorResults, days) => {
    const loadData = sensorResults.find(r => r.sensorId === sensorConfig.energySensor)?.data || [];
    const solarData = sensorResults.find(r => r.sensorId === sensorConfig.solarSensor)?.data || [];
    const exportData = sensorResults.find(r => r.sensorId === sensorConfig.exportSensor)?.data || [];
    const purchasePriceData = sensorResults.find(r => r.sensorId === sensorConfig.purchasePriceSensor)?.data || [];
    const feedinPriceData = sensorResults.find(r => r.sensorId === sensorConfig.feedinPriceSensor)?.data || [];

    if (loadData.length === 0) {
      console.warn('No sensor data available, using mock data');
      return generateMockData(days.toString());
    }

    const dailyData = {};
    
    const parseValue = (state, unit) => {
      const value = parseFloat(state.state);
      if (isNaN(value)) return 0;
      if (unit === 'MWh') return value * 1000;
      return value;
    };

    let prevLoad = null;
    loadData.forEach((state) => {
      const date = new Date(state.last_changed).toISOString().split('T')[0];
      const value = parseValue(state, state.attributes?.unit_of_measurement);
      
      if (prevLoad !== null && value > prevLoad) {
        const delta = value - prevLoad;
        if (!dailyData[date]) dailyData[date] = { date, load: 0, solar: 0, export: 0 };
        dailyData[date].load += delta;
      }
      prevLoad = value;
    });

    let prevSolar = null;
    solarData.forEach((state) => {
      const date = new Date(state.last_changed).toISOString().split('T')[0];
      const value = parseValue(state, state.attributes?.unit_of_measurement);
      
      if (prevSolar !== null && value > prevSolar) {
        const delta = value - prevSolar;
        if (!dailyData[date]) dailyData[date] = { date, load: 0, solar: 0, export: 0 };
        dailyData[date].solar += delta;
      }
      prevSolar = value;
    });

    let prevExport = null;
    exportData.forEach((state) => {
      const date = new Date(state.last_changed).toISOString().split('T')[0];
      const value = parseValue(state, state.attributes?.unit_of_measurement);
      
      if (prevExport !== null && value > prevExport) {
        const delta = value - prevExport;
        if (!dailyData[date]) dailyData[date] = { date, load: 0, solar: 0, export: 0 };
        dailyData[date].export += delta;
      }
      prevExport = value;
    });

    const dailyUsage = Object.values(dailyData).map(day => {
      const gridImport = Math.max(0, day.load - day.solar);
      const gridExport = day.export > 0 ? day.export : Math.max(0, day.solar - day.load);
      const selfConsumption = Math.min(day.load, day.solar);
      const peakUsage = gridImport * 0.4;
      const offPeakUsage = gridImport * 0.6;
      
      return {
        date: day.date,
        gridImport,
        gridExport,
        solarProduction: day.solar,
        selfConsumption,
        peakUsage,
        offPeakUsage,
      };
    });

    const totalImport = dailyUsage.reduce((sum, d) => sum + d.gridImport, 0);
    const totalExport = dailyUsage.reduce((sum, d) => sum + d.gridExport, 0);
    const totalSolar = dailyUsage.reduce((sum, d) => sum + d.solarProduction, 0);
    const totalSelfConsumption = dailyUsage.reduce((sum, d) => sum + d.selfConsumption, 0);

    const avgPurchasePrice = purchasePriceData.length > 0
      ? purchasePriceData.reduce((sum, s) => sum + parseFloat(s.state), 0) / purchasePriceData.length
      : 0.15;

    const avgFeedinPrice = feedinPriceData.length > 0
      ? feedinPriceData.reduce((sum, s) => sum + parseFloat(s.state), 0) / feedinPriceData.length
      : 0.05;

    const amberImportCost = totalImport * avgPurchasePrice;
    const amberExportCredit = totalExport * avgFeedinPrice;
    const amberNetCost = amberImportCost - amberExportCredit;
    const amberSubscription = 25.00;
    const amberTotal = amberNetCost + amberSubscription;

    const plans = [
      {
        id: 'energyaustralia',
        name: 'EnergyAustralia',
        planName: 'EV Night Boost',
        dailySupply: 1.10,
        rates: { peak: 0.32, shoulder: 0.25, offPeak: 0.07 },
        feedIn: 0.05,
        color: '#FF6B6B',
      },
      {
        id: 'agl',
        name: 'AGL',
        planName: 'Night Saver EV',
        dailySupply: 1.15,
        rates: { peak: 0.35, offPeak: 0.08 },
        feedIn: 0.05,
        color: '#4ECDC4',
      },
      {
        id: 'ovo',
        name: 'OVO Energy',
        planName: 'The EV Plan',
        dailySupply: 1.05,
        rates: { peak: 0.30, offPeak: 0.08, superOffPeak: 0.00 },
        feedIn: 0.06,
        color: '#95E1D3',
      },
    ];

    const planComparisons = plans.map(plan => {
      const supplyCost = plan.dailySupply * days;
      const peakKwh = dailyUsage.reduce((sum, d) => sum + d.peakUsage, 0);
      const offPeakKwh = dailyUsage.reduce((sum, d) => sum + d.offPeakUsage, 0);
      
      let usageCost = 0;
      if (plan.rates.superOffPeak !== undefined) {
        usageCost = (peakKwh * 0.7 * plan.rates.peak) + 
                    (offPeakKwh * plan.rates.offPeak) +
                    (peakKwh * 0.3 * 0);
      } else if (plan.rates.shoulder !== undefined) {
        usageCost = (peakKwh * plan.rates.peak) + 
                    (offPeakKwh * 0.5 * plan.rates.shoulder) +
                    (offPeakKwh * 0.5 * plan.rates.offPeak);
      } else {
        usageCost = (peakKwh * plan.rates.peak) + (offPeakKwh * plan.rates.offPeak);
      }
      
      const exportCredit = totalExport * plan.feedIn;
      const netCost = supplyCost + usageCost - exportCredit;

      return {
        ...plan,
        supplyCost,
        usageCost,
        exportCredit,
        totalCost: netCost,
        vsAmber: netCost - amberTotal,
      };
    });

    return {
      dailyUsage,
      summary: {
        days,
        totalImport,
        totalExport,
        totalSolar,
        totalSelfConsumption,
        selfConsumptionRate: totalSolar > 0 ? (totalSelfConsumption / totalSolar) * 100 : 0,
      },
      amber: {
        importCost: amberImportCost,
        exportCredit: amberExportCredit,
        netCost: amberNetCost,
        subscription: amberSubscription,
        total: amberTotal,
        avgPurchasePrice,
        avgFeedinPrice,
      },
      plans: planComparisons,
    };
  };

  const generateMockData = (days) => {
    const daysNum = parseInt(days);
    
    const dailyUsage = Array.from({ length: daysNum }, (_, i) => {
      const date = new Date();
      date.setDate(date.getDate() - (daysNum - i - 1));
      
      return {
        date: date.toISOString().split('T')[0],
        gridImport: 8 + Math.random() * 4,
        gridExport: 12 + Math.random() * 8,
        solarProduction: 25 + Math.random() * 10,
        selfConsumption: 10 + Math.random() * 5,
        peakUsage: 2 + Math.random() * 2,
        offPeakUsage: 6 + Math.random() * 3,
      };
    });

    const totalImport = dailyUsage.reduce((sum, d) => sum + d.gridImport, 0);
    const totalExport = dailyUsage.reduce((sum, d) => sum + d.gridExport, 0);
    const totalSolar = dailyUsage.reduce((sum, d) => sum + d.solarProduction, 0);
    const totalSelfConsumption = dailyUsage.reduce((sum, d) => sum + d.selfConsumption, 0);

    const amberImportCost = totalImport * 0.15;
    const amberExportCredit = totalExport * 0.05;
    const amberNetCost = amberImportCost - amberExportCredit;
    const amberSubscription = 25.00;
    const amberTotal = amberNetCost + amberSubscription;

    const plans = [
      {
        id: 'energyaustralia',
        name: 'EnergyAustralia',
        planName: 'EV Night Boost',
        dailySupply: 1.10,
        rates: { peak: 0.32, shoulder: 0.25, offPeak: 0.07 },
        feedIn: 0.05,
        color: '#FF6B6B',
        supplyCost: 1.10 * daysNum,
        usageCost: totalImport * 0.20,
        exportCredit: totalExport * 0.05,
        totalCost: (1.10 * daysNum) + (totalImport * 0.20) - (totalExport * 0.05),
        vsAmber: 0,
      },
      {
        id: 'agl',
        name: 'AGL',
        planName: 'Night Saver EV',
        dailySupply: 1.15,
        rates: { peak: 0.35, offPeak: 0.08 },
        feedIn: 0.05,
        color: '#4ECDC4',
        supplyCost: 1.15 * daysNum,
        usageCost: totalImport * 0.22,
        exportCredit: totalExport * 0.05,
        totalCost: (1.15 * daysNum) + (totalImport * 0.22) - (totalExport * 0.05),
        vsAmber: 0,
      },
      {
        id: 'ovo',
        name: 'OVO Energy',
        planName: 'The EV Plan',
        dailySupply: 1.05,
        rates: { peak: 0.30, offPeak: 0.08, superOffPeak: 0.00 },
        feedIn: 0.06,
        color: '#95E1D3',
        supplyCost: 1.05 * daysNum,
        usageCost: totalImport * 0.18,
        exportCredit: totalExport * 0.06,
        totalCost: (1.05 * daysNum) + (totalImport * 0.18) - (totalExport * 0.06),
        vsAmber: 0,
      },
    ];

    plans.forEach(plan => {
      plan.vsAmber = plan.totalCost - amberTotal;
    });

    return {
      dailyUsage,
      summary: {
        days: daysNum,
        totalImport,
        totalExport,
        totalSolar,
        totalSelfConsumption,
        selfConsumptionRate: (totalSelfConsumption / totalSolar) * 100,
      },
      amber: {
        importCost: amberImportCost,
        exportCredit: amberExportCredit,
        netCost: amberNetCost,
        subscription: amberSubscription,
        total: amberTotal,
        avgPurchasePrice: 0.15,
        avgFeedinPrice: 0.05,
      },
      plans,
    };
  };

  const formatCurrency = (value) => {
    return new Intl.NumberFormat('en-AU', {
      style: 'currency',
      currency: 'AUD',
      minimumFractionDigits: 2,
    }).format(value);
  };

  const formatKwh = (value) => {
    return `${value.toFixed(1)} kWh`;
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gradient-to-br from-slate-900 via-purple-900 to-slate-900">
        <div className="text-center">
          <div className="animate-spin rounded-full h-16 w-16 border-4 border-emerald-400 border-t-transparent mx-auto mb-4"></div>
          <p className="text-emerald-300 font-medium">Loading your energy data from Home Assistant...</p>
          <p className="text-slate-500 text-sm mt-2">Fetching sensor history...</p>
        </div>
      </div>
    );
  }

  if (!usageData) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gradient-to-br from-slate-900 via-purple-900 to-slate-900">
        <div className="text-center max-w-md">
          <div className="text-6xl mb-4">⚠️</div>
          <p className="text-red-400 font-medium mb-2">Failed to load data</p>
          <p className="text-slate-400 text-sm">Check that your sensors are configured in the integration.</p>
          <button 
            onClick={fetchHomeAssistantData}
            className="mt-4 px-6 py-3 bg-emerald-500 text-white rounded-xl font-semibold hover:bg-emerald-600 transition-all"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-slate-900 via-purple-900 to-slate-900 text-white p-6">
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap');
        
        * {
          font-family: 'Outfit', sans-serif;
        }
        
        .mono {
          font-family: 'JetBrains Mono', monospace;
        }
        
        .glow {
          box-shadow: 0 0 20px rgba(16, 185, 129, 0.3);
        }
        
        .card {
          background: rgba(30, 41, 59, 0.7);
          backdrop-filter: blur(10px);
          border: 1px solid rgba(148, 163, 184, 0.2);
          transition: all 0.3s ease;
        }
        
        .card:hover {
          border-color: rgba(16, 185, 129, 0.4);
          transform: translateY(-2px);
        }
        
        .stat-card {
          background: linear-gradient(135deg, rgba(16, 185, 129, 0.1) 0%, rgba(139, 92, 246, 0.1) 100%);
          border: 1px solid rgba(16, 185, 129, 0.3);
        }
        
        .plan-card {
          background: rgba(30, 41, 59, 0.5);
          border: 2px solid transparent;
          transition: all 0.3s ease;
        }
        
        .plan-card:hover {
          border-color: rgba(16, 185, 129, 0.5);
          background: rgba(30, 41, 59, 0.8);
        }
        
        .plan-card.selected {
          border-color: #10b981;
          background: rgba(16, 185, 129, 0.1);
        }
      `}</style>

      {/* Header */}
      <div className="max-w-7xl mx-auto mb-8">
        {error && (
          <div className="bg-yellow-900/30 border border-yellow-600/50 rounded-xl p-4 mb-4 flex items-start gap-3">
            <span className="text-2xl">⚠️</span>
            <div>
              <p className="text-yellow-300 font-semibold">Using Simulated Data</p>
              <p className="text-yellow-200/70 text-sm mt-1">{error}</p>
              <button 
                onClick={fetchHomeAssistantData}
                className="mt-2 text-xs px-3 py-1 bg-yellow-600 hover:bg-yellow-700 rounded-lg transition-all"
              >
                Retry Connection
              </button>
            </div>
          </div>
        )}
        
        <h1 className="text-5xl font-bold mb-2 bg-gradient-to-r from-emerald-400 to-purple-400 bg-clip-text text-transparent">
          ⚡ Energy Plan Analysis
        </h1>
        <p className="text-slate-400 text-lg">Your actual usage vs. alternative plans</p>
        
        {usageData.amber.avgPurchasePrice && (
          <div className="mt-3 text-sm text-slate-500">
            <span className="text-emerald-400 font-mono">
              ✓ Connected to Home Assistant
            </span>
            {' • '}
            Avg purchase: ${usageData.amber.avgPurchasePrice.toFixed(3)}/kWh
            {' • '}
            Avg feed-in: ${usageData.amber.avgFeedinPrice.toFixed(3)}/kWh
          </div>
        )}
      </div>

      {/* Date Range Selector */}
      <div className="max-w-7xl mx-auto mb-8">
        <div className="card rounded-2xl p-6">
          <label className="block text-sm font-semibold text-emerald-400 mb-3">Analysis Period</label>
          <div className="flex gap-3 flex-wrap">
            {['7', '30', '90', '365'].map((days) => (
              <button
                key={days}
                onClick={() => setDateRange(days)}
                className={`px-6 py-3 rounded-xl font-semibold transition-all ${
                  dateRange === days
                    ? 'bg-emerald-500 text-white glow'
                    : 'bg-slate-700/50 text-slate-300 hover:bg-slate-600/50'
                }`}
              >
                Last {days} days
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Summary Stats */}
      <div className="max-w-7xl mx-auto mb-8 grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="stat-card rounded-2xl p-6">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-semibold text-emerald-400">Grid Import</span>
            <span className="text-2xl">⬇️</span>
          </div>
          <div className="text-3xl font-bold mono">{formatKwh(usageData.summary.totalImport)}</div>
          <div className="text-sm text-slate-400 mt-1">What you purchased</div>
        </div>

        <div className="stat-card rounded-2xl p-6">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-semibold text-purple-400">Grid Export</span>
            <span className="text-2xl">⬆️</span>
          </div>
          <div className="text-3xl font-bold mono">{formatKwh(usageData.summary.totalExport)}</div>
          <div className="text-sm text-slate-400 mt-1">What you sold</div>
        </div>

        <div className="stat-card rounded-2xl p-6">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-semibold text-yellow-400">Solar Production</span>
            <span className="text-2xl">☀️</span>
          </div>
          <div className="text-3xl font-bold mono">{formatKwh(usageData.summary.totalSolar)}</div>
          <div className="text-sm text-slate-400 mt-1">Total generated</div>
        </div>

        <div className="stat-card rounded-2xl p-6">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-semibold text-blue-400">Self-Consumption</span>
            <span className="text-2xl">🔋</span>
          </div>
          <div className="text-3xl font-bold mono">{usageData.summary.selfConsumptionRate.toFixed(0)}%</div>
          <div className="text-sm text-slate-400 mt-1">Solar used directly</div>
        </div>
      </div>

      {/* Daily Usage Chart */}
      <div className="max-w-7xl mx-auto mb-8">
        <div className="card rounded-2xl p-6">
          <h2 className="text-2xl font-bold mb-6">Daily Energy Flow</h2>
          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={usageData.dailyUsage}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.1)" />
              <XAxis 
                dataKey="date" 
                stroke="#94a3b8"
                tick={{ fill: '#94a3b8' }}
              />
              <YAxis 
                stroke="#94a3b8"
                tick={{ fill: '#94a3b8' }}
                label={{ value: 'kWh', angle: -90, position: 'insideLeft', fill: '#94a3b8' }}
              />
              <Tooltip 
                contentStyle={{ 
                  backgroundColor: 'rgba(30, 41, 59, 0.95)', 
                  border: '1px solid rgba(16, 185, 129, 0.3)',
                  borderRadius: '12px',
                  color: '#fff'
                }}
              />
              <Legend />
              <Line type="monotone" dataKey="gridImport" stroke="#10b981" strokeWidth={2} name="Grid Import" />
              <Line type="monotone" dataKey="gridExport" stroke="#a78bfa" strokeWidth={2} name="Grid Export" />
              <Line type="monotone" dataKey="solarProduction" stroke="#fbbf24" strokeWidth={2} name="Solar Production" />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Amber Current Costs */}
      <div className="max-w-7xl mx-auto mb-8">
        <div className="card rounded-2xl p-6">
          <div className="flex items-center justify-between mb-6">
            <h2 className="text-2xl font-bold">Your Current Plan: Amber Electric</h2>
            <div className="text-3xl font-bold mono text-emerald-400">
              {formatCurrency(usageData.amber.total)}
            </div>
          </div>
          
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4">
            <div className="bg-slate-800/50 rounded-xl p-4">
              <div className="text-sm text-slate-400 mb-1">Import Cost</div>
              <div className="text-xl font-bold mono text-red-400">
                {formatCurrency(usageData.amber.importCost)}
              </div>
              <div className="text-xs text-slate-500 mt-1">
                {formatKwh(usageData.summary.totalImport)} @ avg ${usageData.amber.avgPurchasePrice.toFixed(2)}/kWh
              </div>
            </div>

            <div className="bg-slate-800/50 rounded-xl p-4">
              <div className="text-sm text-slate-400 mb-1">Export Credits</div>
              <div className="text-xl font-bold mono text-emerald-400">
                -{formatCurrency(usageData.amber.exportCredit)}
              </div>
              <div className="text-xs text-slate-500 mt-1">
                {formatKwh(usageData.summary.totalExport)} @ avg ${usageData.amber.avgFeedinPrice.toFixed(2)}/kWh
              </div>
            </div>

            <div className="bg-slate-800/50 rounded-xl p-4">
              <div className="text-sm text-slate-400 mb-1">Net Energy Cost</div>
              <div className="text-xl font-bold mono">
                {formatCurrency(usageData.amber.netCost)}
              </div>
              <div className="text-xs text-slate-500 mt-1">
                After export credits
              </div>
            </div>

            <div className="bg-slate-800/50 rounded-xl p-4">
              <div className="text-sm text-slate-400 mb-1">Subscription</div>
              <div className="text-xl font-bold mono">
                {formatCurrency(usageData.amber.subscription)}
              </div>
              <div className="text-xs text-slate-500 mt-1">
                Monthly fee
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Plan Comparisons */}
      <div className="max-w-7xl mx-auto mb-8">
        <h2 className="text-2xl font-bold mb-6">Alternative Plans</h2>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          {usageData.plans.map((plan) => (
            <div 
              key={plan.id}
              className={`plan-card rounded-2xl p-6 cursor-pointer ${selectedPlan === plan.id ? 'selected' : ''}`}
              onClick={() => setSelectedPlan(selectedPlan === plan.id ? null : plan.id)}
            >
              <div className="flex items-start justify-between mb-4">
                <div>
                  <h3 className="text-xl font-bold">{plan.name}</h3>
                  <p className="text-sm text-slate-400">{plan.planName}</p>
                </div>
                <div className={`w-3 h-3 rounded-full`} style={{ backgroundColor: plan.color }}></div>
              </div>

              <div className="mb-4">
                <div className="text-3xl font-bold mono mb-1" style={{ color: plan.color }}>
                  {formatCurrency(plan.totalCost)}
                </div>
                <div className={`text-sm font-semibold ${plan.vsAmber < 0 ? 'text-emerald-400' : 'text-red-400'}`}>
                  {plan.vsAmber < 0 ? '✓ ' : '✗ '}
                  {formatCurrency(Math.abs(plan.vsAmber))} 
                  {plan.vsAmber < 0 ? ' cheaper' : ' more expensive'}
                </div>
              </div>

              <div className="space-y-2 text-sm">
                <div className="flex justify-between">
                  <span className="text-slate-400">Daily supply</span>
                  <span className="mono">{formatCurrency(plan.supplyCost)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Usage cost</span>
                  <span className="mono">{formatCurrency(plan.usageCost)}</span>
                </div>
                <div className="flex justify-between">
                  <span className="text-slate-400">Export credits</span>
                  <span className="mono text-emerald-400">-{formatCurrency(plan.exportCredit)}</span>
                </div>
              </div>

              {selectedPlan === plan.id && (
                <div className="mt-4 pt-4 border-t border-slate-700 space-y-2 text-sm">
                  <div className="font-semibold text-emerald-400 mb-2">Rate Details</div>
                  {Object.entries(plan.rates).map(([period, rate]) => (
                    <div key={period} className="flex justify-between">
                      <span className="text-slate-400 capitalize">
                        {period.replace(/([A-Z])/g, ' $1').trim()}
                      </span>
                      <span className="mono">{formatCurrency(rate)}/kWh</span>
                    </div>
                  ))}
                  <div className="flex justify-between">
                    <span className="text-slate-400">Feed-in tariff</span>
                    <span className="mono">{formatCurrency(plan.feedIn)}/kWh</span>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Comparison Chart */}
      <div className="max-w-7xl mx-auto">
        <div className="card rounded-2xl p-6">
          <h2 className="text-2xl font-bold mb-6">Cost Comparison</h2>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={[
              { name: 'Amber', cost: usageData.amber.total, fill: '#10b981' },
              ...usageData.plans.map(p => ({ name: p.name, cost: p.totalCost, fill: p.color }))
            ]}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(148, 163, 184, 0.1)" />
              <XAxis dataKey="name" stroke="#94a3b8" tick={{ fill: '#94a3b8' }} />
              <YAxis stroke="#94a3b8" tick={{ fill: '#94a3b8' }} />
              <Tooltip 
                contentStyle={{ 
                  backgroundColor: 'rgba(30, 41, 59, 0.95)', 
                  border: '1px solid rgba(16, 185, 129, 0.3)',
                  borderRadius: '12px',
                  color: '#fff'
                }}
                formatter={(value) => formatCurrency(value)}
              />
              <Bar dataKey="cost" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      </div>

      {/* Footer Note */}
      <div className="max-w-7xl mx-auto mt-8 text-center text-sm text-slate-500">
        <p className="mb-2">💡 Click on a plan card to see detailed rate breakdown.</p>
        <p className="text-emerald-400 mt-2">Sensor data is automatically retrieved from your integration configuration.</p>
      </div>
    </div>
  );
}
