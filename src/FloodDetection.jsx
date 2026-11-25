import React, { useEffect, useRef, useState } from 'react';
import { Activity, ArrowUpRight, CloudRain, Download, ShieldCheck } from 'lucide-react';
import { OriginalImageDownloads } from './OriginalImageDownloads.jsx';

export function FloodWorkspace({ submitAnalysis, busy, result, error, setResult, api }) {
  const [mode, setMode] = useState('comparison');
  const [sample, setSample] = useState(false);
  const [previews, setPreviews] = useState({ before_file: null, after_file: null, file: null });
  const previewUrls = useRef({});

  useEffect(() => () => Object.values(previewUrls.current).forEach(url => url && URL.revokeObjectURL(url)), []);

  function setPreview(key, file) {
    if (previewUrls.current[key]) URL.revokeObjectURL(previewUrls.current[key]);
    const url = file ? URL.createObjectURL(file) : null;
    previewUrls.current[key] = url;
    setPreviews(current => ({ ...current, [key]: file ? { url, name: file.name } : null }));
  }

  function clearPreviews() {
    Object.values(previewUrls.current).forEach(url => url && URL.revokeObjectURL(url));
    previewUrls.current = {};
    setPreviews({ before_file: null, after_file: null, file: null });
  }

  function switchMode(next) {
    setMode(next);
    setSample(false);
    setResult(null);
    clearPreviews();
  }

  function updateFile(event) {
    setSample(false);
    setResult(null);
    setPreview(event.target.name, event.target.files?.[0]);
  }

  async function loadSample() {
    const files = mode === 'comparison'
      ? [['before_file', 'demo_scene.png'], ['after_file', 'demo_scene_after.png']]
      : [['file', 'demo_scene.png']];
    for (const [field, name] of files) {
      const input = document.querySelector(`input[name="${field}"]`);
      const blob = await fetch(`/${name}`).then(response => response.blob());
      const file = new File([blob], name, { type: 'image/png' });
      const transfer = new DataTransfer();
      transfer.items.add(file);
      input.files = transfer.files;
      setPreview(field, file);
    }
    setSample(true);
    setResult(null);
  }

  function uploadCard(field, title, dateField, dateLabel) {
    const preview = previews[field];
    return <div className="change-upload-card" key={field}>
      <div className="comparison-label">{title}</div>
      <div className="comparison-stage">
        {preview?.url ? <img src={preview.url} alt={title} /> : <span>Select an image to preview</span>}
      </div>
      <input name={field} type="file" accept="image/*" required onChange={updateFile} />
      <label className="date-field">{dateLabel}<input name={dateField} type="date" /></label>
    </div>;
  }

  return <>
    <div className="welcome compact">
      <div><div className="eyebrow">IMAGE ANALYSIS / FLOOD DETECTION</div><h1>Flood detection</h1><p>Screen one image for possible water, or compare before and after imagery.</p></div>
      <span className="tag amber">HEURISTIC BASELINE</span>
    </div>
    <div className="workgrid">
      <form className="panel uploadpanel" onSubmit={event => submitAnalysis(event, 'flood')}>
        <div className="panelhead"><div><div className="eyebrow">ANALYSIS SETUP</div><h3>Flood screening</h3></div><span className="step">01 / 02</span></div>
        <div className="flood-mode-switch">
          <button type="button" className={mode === 'comparison' ? 'primary' : 'secondary'} onClick={() => switchMode('comparison')}>Before and after</button>
          <button type="button" className={mode === 'single' ? 'primary' : 'secondary'} onClick={() => switchMode('single')}>Single-image screening</button>
        </div>
        <label className="fieldlabel">Analysis name<input name="name" placeholder={mode === 'comparison' ? 'e.g. Flood comparison — July' : 'e.g. Possible water screening'} /></label>
        <label className="fieldlabel">Location<input name="location" placeholder="Search or name a location" /></label>
        <input type="hidden" name="source" value={sample ? 'SAMPLE DATA' : 'USER UPLOAD'} />
        {mode === 'comparison'
          ? <div className="change-upload-grid">{uploadCard('before_file', 'Before Flood image', 'before_date', 'Before acquisition date (optional)')}{uploadCard('after_file', 'After Flood image', 'after_date', 'After acquisition date (optional)')}</div>
          : <div className="change-upload-grid single-flood-upload">{uploadCard('file', 'Image to screen', 'image_date', 'Image date (optional)')}</div>}
        <button type="button" className="textbutton sample-button" onClick={loadSample}>{mode === 'comparison' ? 'Load synthetic demo pair' : 'Load synthetic demo image'}</button>
        {sample && <span className="tag amber">SYNTHETIC DEMO — NOT A FLOOD EVENT</span>}
        {error && <div className="error">{error}</div>}
        <div className="notice"><ShieldCheck size={15} /><span>Possible water is a heuristic screening result, not confirmed flooding or a trained AI prediction. Geospatial area is not calculated from these uploads.</span></div>
        <button className="primary full" disabled={busy}>{busy ? 'Analyzing images…' : mode === 'comparison' ? 'Compare flood images' : 'Screen for possible water'} <ArrowUpRight size={16} /></button>
      </form>
      <div className="rightcol">
        <div className="panel resultpanel">
          <div className="panelhead"><div><div className="eyebrow">FLOOD SCREENING OUTPUT</div><h3>{result ? 'Analysis complete' : 'Awaiting imagery'}</h3></div>{result && <span className={result.result?.source === 'SAMPLE DATA' ? 'tag amber' : 'tag green'}>{result.result?.source === 'SAMPLE DATA' ? 'SYNTHETIC DEMO' : 'COMPLETE'}</span>}</div>
          {result ? <FloodResultView result={result} data={result.result} api={api} /> : <div className="previewempty"><div className="previeworbit"><CloudRain size={27} /></div><b>Select one image or a before/after pair</b><p>Upload previews update immediately. Comparison results require a successful visual alignment check.</p></div>}
        </div>
        <div className="panel methodnote"><div className="noteicon"><Activity size={17} /></div><div><b>Interpretation</b><p>Clouds, shadows, muddy soil, different seasons, image quality and residual misalignment can cause missed or false water detections. Compare matched images and verify their location and alignment.</p></div></div>
      </div>
    </div>
  </>;
}

function FloodImagePanel({ label, image, name, date, note }) {
  return <div className="comparison-card">
    <div className="comparison-label">{label}</div>
    <div className="comparison-stage">{image ? <img src={image} alt={label} /> : <span>Image unavailable in this saved analysis</span>}</div>
    <div className="comparison-meta"><b>{name || label}</b>{date && <small>Acquired {date}</small>}{note && <small>{note}</small>}</div>
  </div>;
}

function FloodReportLink({ result, api }) {
  return <a className="secondary download" href={`${api}/analysis/${result.id}/report`} onClick={async event => {
    event.preventDefault();
    const response = await fetch(event.currentTarget.href, { headers: { Authorization: `Bearer ${localStorage.token}` } });
    const blob = await response.blob();
    const anchor = document.createElement('a');
    anchor.href = URL.createObjectURL(blob);
    anchor.download = 'satquery-report.pdf';
    anchor.click();
  }}><Download size={15} /> Download PDF report</a>;
}

export function FloodResultView({ result, data, api }) {
  const [view, setView] = useState('overlay');
  if (data.analysis_mode === 'comparison') {
    const valid = data.comparison_valid && Number.isFinite(data.new_water_percent);
    const sample = data.source === 'SAMPLE DATA';
    return <>
      <div className="comparison-section-heading"><div><div className="eyebrow">BEFORE &amp; AFTER</div><h3>Flood Change Analysis</h3></div>{sample && <span className="tag amber">SYNTHETIC DEMO</span>}</div>
      <div className="comparison-grid flood-comparison-grid"><FloodImagePanel label="Before Flood" image={data.before_image} name={data.before_filename} date={data.before_date} /><FloodImagePanel label="After Flood" image={data.after_image} name={data.after_filename} date={data.after_date} /></div>
      <div className={valid ? 'flood-alignment-note' : 'flood-alignment-warning'}><b>{valid ? 'Visual alignment check passed' : 'Comparison alignment warning'}</b><p>{data.alignment_warning}</p><small>Map coordinates/georeferencing are unavailable, so geographic alignment is not independently confirmed.</small></div>
      <div className="change-stat"><span>Possible newly water-covered pixels</span><b>{valid ? `${data.new_water_percent}%` : 'Not calculated'}</b><small>{valid ? `${Number(data.new_water_pixels).toLocaleString()} of ${Number(data.compared_pixels).toLocaleString()} aligned pixels; water present in both images is excluded.` : 'The comparison did not pass alignment checks, so no change percentage is reported.'}</small></div>
      <div className="change-overlay"><h4>Flood-change overlay</h4>{data.image && <img src={data.image} alt="Magenta overlay of newly detected possible water" />}<p>Magenta marks water detected after the event but not in the before-image mask. This is possible new water coverage, not confirmed flooding.</p></div>
      <div className="flood-mask-section"><div className="eyebrow">WATER MASKS</div><div className="comparison-grid"><FloodImagePanel label="Before water mask" image={data.before_mask_image} name={data.before_filename} date={data.before_date} note={`${data.before_water_percent ?? '—'}% possible water in this image`} /><FloodImagePanel label="After water mask" image={data.after_mask_image} name={data.after_filename} date={data.after_date} note={`${data.after_water_percent ?? '—'}% possible water in this image`} /></div></div>
      <div className="method"><b>METHOD</b><p>{data.method}</p><b>LIMITATIONS</b><p>{data.limitations}</p><b>AREA ESTIMATE</b><p>{data.area_estimate_note}</p></div>
      <OriginalImageDownloads result={result} api={api} roles={[{ key: 'before', label: 'before flood image' }, { key: 'after', label: 'after flood image' }]} />
      <FloodReportLink result={result} api={api} />
    </>;
  }

  const images = { overlay: data.image, original: data.original_image, mask: data.mask_image };
  const labels = { overlay: 'Detection overlay · magenta = possible water', original: 'Original uploaded image', mask: 'Possible-water mask' };
  const modes = data.original_image ? [['overlay', 'Overlay'], ['original', 'Original'], ['mask', 'Mask']] : [['overlay', 'Overlay']];
  const number = value => Number.isFinite(value) ? value.toLocaleString() : '—';
  return <>
    <div className="comparison-section-heading"><div><div className="eyebrow">SINGLE IMAGE</div><h3>Possible-water screening</h3></div>{data.source === 'SAMPLE DATA' && <span className="tag amber">SYNTHETIC DEMO</span>}</div>
    <div className="flood-views">{modes.map(([key, label]) => <button type="button" className={view === key ? 'primary' : 'secondary'} onClick={() => setView(key)} key={key}>{label}</button>)}</div>
    <div className="resultimage"><img src={images[view] || data.image} alt={labels[view]} /><span className="tag green">{labels[view]}</span></div>
    <div className="resultstats"><div><span>Possible water</span><b>{data.possible_water_percent}%</b></div><div><span>Detected pixels</span><b>{number(data.possible_water_pixels)}</b></div><div><span>Total image pixels</span><b>{number(data.total_image_pixels)}</b></div></div>
    <div className="comparison-meta">{data.filename && <b>{data.filename}</b>}{data.image_date && <small>Image date: {data.image_date}</small>}</div>
    <div className="method"><b>AREA ESTIMATE</b><p>{data.area_estimate_note}</p><b>METHOD</b><p>{data.method}</p><b>LIMITATIONS</b><p>{data.limitations}</p></div>
    <OriginalImageDownloads result={result} api={api} roles={[{ key: 'original', label: 'uploaded image' }]} />
    <FloodReportLink result={result} api={api} />
  </>;
}
