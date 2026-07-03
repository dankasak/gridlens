class ElectricityEnergyFlowCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this._config = {};
    this._hass = null;
    this._data = null;
  }

  setConfig(config) {
    this._config = config;
    this.render();
  }

  set hass(hass) {
    this._hass = hass;
    this.fetchData();
  }

  async fetchData() {
    if (!this._hass) return;

    try {
      const response = await fetch('/api/grid_lens/plan_data');
      if (!response.ok) throw new Error('Failed to fetch data');
      const data = await response.json();
      this._data = data.energy_flows;
      this.render();
    } catch (error) {
      console.error('Error fetching energy flow data:', error);
      this.renderError(error.message);
    }
  }

  render() {
    if (!this._config || !this._hass) return;

    const styles = `
      <style>
        :host {
          display: block;
        }
        .card-header {
          padding: 16px;
          font-size: 18px;
          font-weight: 500;
          color: var(--primary-text-color);
        }
        .stats-bar {
          display: flex;
          justify-content: space-around;
          padding: 8px 16px;
          background: var(--secondary-background-color);
          border-bottom: 1px solid var(--divider-color);
        }
        .stat {
          text-align: center;
        }
        .stat-value {
          font-size: 20px;
          font-weight: 600;
          color: var(--primary-text-color);
        }
        .stat-label {
          font-size: 11px;
          color: var(--secondary-text-color);
          margin-top: 2px;
        }
        .chart-container {
          position: relative;
          padding: 16px;
          height: 200px;
        }
        canvas {
          width: 100%;
          height: 100%;
        }
        .legend {
          display: flex;
          flex-wrap: wrap;
          gap: 12px;
          padding: 8px 16px;
          font-size: 11px;
        }
        .legend-item {
          display: flex;
          align-items: center;
          gap: 6px;
        }
        .legend-color {
          width: 12px;
          height: 12px;
          border-radius: 2px;
        }
        .error, .loading {
          padding: 16px;
          text-align: center;
          color: var(--secondary-text-color);
        }
      </style>
    `;

    if (!this._data || !this._data.hourly) {
      this.shadowRoot.innerHTML = `
        ${styles}
        <ha-card>
          <div class="card-header">Energy Flow</div>
          <div class="loading">Loading energy data...</div>
        </ha-card>
      `;
      return;
    }

    const summary = this._data.summary || {};
    const hourly = this._data.hourly || [];

    this.shadowRoot.innerHTML = `
      ${styles}
      <ha-card>
        <div class="card-header">${this._config.title || 'Energy In & Out'}</div>
        
        <div class="stats-bar">
          <div class="stat">
            <div class="stat-value">${summary.total_solar?.toFixed(1) || '0.0'} kWh</div>
            <div class="stat-label">Solar</div>
          </div>
          <div class="stat">
            <div class="stat-value">${summary.total_import?.toFixed(1) || '0.0'} kWh</div>
            <div class="stat-label">Import</div>
          </div>
          <div class="stat">
            <div class="stat-value">${summary.total_export?.toFixed(1) || '0.0'} kWh</div>
            <div class="stat-label">Export</div>
          </div>
          ${summary.total_battery_charge > 0 ? `
            <div class="stat">
              <div class="stat-value">${summary.total_battery_charge?.toFixed(1) || '0.0'} kWh</div>
              <div class="stat-label">Battery</div>
            </div>
          ` : ''}
        </div>
        
        <div class="chart-container">
          <canvas id="energyChart"></canvas>
        </div>
        
        <div class="legend">
          <div class="legend-item">
            <div class="legend-color" style="background: #FFA726;"></div>
            <span>Solar PV</span>
          </div>
          <div class="legend-item">
            <div class="legend-color" style="background: #42A5F5;"></div>
            <span>Grid Export</span>
          </div>
          <div class="legend-item">
            <div class="legend-color" style="background: #EF5350;"></div>
            <span>Grid Import</span>
          </div>
          <div class="legend-item">
            <div class="legend-color" style="background: #AB47BC;"></div>
            <span>Battery Charge</span>
          </div>
          <div class="legend-item">
            <div class="legend-color" style="background: #EC407A;"></div>
            <span>Battery Discharge</span>
          </div>
        </div>
      </ha-card>
    `;

    // Draw chart after render
    requestAnimationFrame(() => this.drawChart(hourly));
  }

  drawChart(hourly) {
    const canvas = this.shadowRoot.getElementById('energyChart');
    if (!canvas) return;

    const ctx = canvas.getContext('2d');
    const width = canvas.offsetWidth;
    const height = canvas.offsetHeight;
    
    // Set canvas resolution
    canvas.width = width * window.devicePixelRatio;
    canvas.height = height * window.devicePixelRatio;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    // Clear canvas
    ctx.clearRect(0, 0, width, height);

    if (!hourly || hourly.length === 0) return;

    // Find max value for scaling (sum of all sources)
    const maxValue = Math.max(
      ...hourly.map(h => 
        (h.solar || 0) + (h.grid_import || 0) + (h.battery_discharge || 0)
      ),
      0.1 // Minimum to avoid divide by zero
    );

    const padding = 30;
    const chartWidth = width - padding * 2;
    const chartHeight = height - padding * 2;
    const barWidth = chartWidth / hourly.length;

    console.log('Drawing chart:', { hours: hourly.length, maxValue, width, height });

    // Draw stacked bars
    hourly.forEach((hour, i) => {
      const x = padding + i * barWidth;
      let y = padding + chartHeight;

      // Helper to draw a bar segment
      const drawBar = (value, color) => {
        if (!value || value <= 0) return;
        const barHeight = (value / maxValue) * chartHeight;
        ctx.fillStyle = color;
        ctx.fillRect(x, y - barHeight, barWidth - 1, barHeight);
        y -= barHeight;
      };

      // Stack from bottom up
      // Order matters for stacking!
      drawBar(hour.battery_discharge || 0, '#EC407A');  // Pink - Battery Discharge
      drawBar(hour.battery_charge || 0, '#AB47BC');  // Purple - Battery Charge  
      drawBar(hour.grid_import || 0, '#EF5350');  // Red - Grid Import
      drawBar(hour.grid_export || 0, '#42A5F5');  // Blue - Grid Export
      drawBar(hour.solar || 0, '#FFA726');  // Orange - Solar PV
    });

    // Draw axes
    ctx.strokeStyle = getComputedStyle(this).getPropertyValue('--divider-color') || '#444';
    ctx.lineWidth = 1;
    
    // Y-axis
    ctx.beginPath();
    ctx.moveTo(padding, padding);
    ctx.lineTo(padding, padding + chartHeight);
    ctx.stroke();
    
    // X-axis
    ctx.beginPath();
    ctx.moveTo(padding, padding + chartHeight);
    ctx.lineTo(padding + chartWidth, padding + chartHeight);
    ctx.stroke();

    // Draw time labels
    ctx.fillStyle = getComputedStyle(this).getPropertyValue('--secondary-text-color') || '#999';
    ctx.font = '10px sans-serif';
    ctx.textAlign = 'center';
    
    // Label every 6 hours
    for (let i = 0; i < hourly.length; i += 6) {
      const x = padding + (i / hourly.length) * chartWidth;
      const hour = new Date(hourly[i].timestamp).getHours();
      ctx.fillText(`${hour}:00`, x, padding + chartHeight + 15);
    }

    // Draw max value label
    ctx.textAlign = 'right';
    ctx.fillText(`${maxValue.toFixed(1)} kW`, padding - 5, padding + 5);
    ctx.fillText('0', padding - 5, padding + chartHeight + 5);
  }

  renderError(message) {
    const styles = `<style>/* same styles */</style>`;
    this.shadowRoot.innerHTML = `
      ${styles}
      <ha-card>
        <div class="card-header">Energy Flow</div>
        <div class="error">Error: ${message}</div>
      </ha-card>
    `;
  }

  getCardSize() {
    return 4;
  }

  static getStubConfig() {
    return {
      title: 'Energy In & Out',
    };
  }
}

customElements.define('grid-lens-flow-card', ElectricityEnergyFlowCard);

window.customCards = window.customCards || [];
window.customCards.push({
  type: 'grid-lens-flow-card',
  name: 'Electricity Energy Flow',
  description: 'Visualize energy flows (solar, grid, battery)',
  preview: true,
});

console.info(
  '%c ELECTRICITY-ENERGY-FLOW-CARD %c v1.0.0 ',
  'color: white; background: #039be5; font-weight: 700;',
  'color: #039be5; background: white; font-weight: 700;',
);
