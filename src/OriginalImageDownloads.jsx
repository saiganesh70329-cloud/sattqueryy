import React, { useState } from 'react';
import { Download } from 'lucide-react';

export function OriginalImageDownloads({ result, api, roles }) {
  const [downloading, setDownloading] = useState('');
  const [error, setError] = useState('');
  if (!result?.result?.original_uploads_available) return null;

  async function download(role) {
    setDownloading(role.key);
    setError('');
    try {
      const response = await fetch(`${api}/analysis/${encodeURIComponent(result.id)}/images/${encodeURIComponent(role.key)}/original`, {
        headers: { Authorization: `Bearer ${localStorage.token}` },
      });
      if (!response.ok) throw new Error(response.status === 404 ? 'Original upload is unavailable.' : 'Could not download the original upload.');
      const blob = await response.blob();
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = objectUrl;
      const disposition = response.headers.get('Content-Disposition') || '';
      const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
      anchor.download = encodedName ? decodeURIComponent(encodedName) : `satquery-${role.key}-original`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    } catch (downloadError) {
      setError(downloadError.message || 'Could not download the original upload.');
    } finally {
      setDownloading('');
    }
  }

  return <div className="original-downloads" aria-label="Original uploaded images">
    {roles.map(role => <button className="secondary" type="button" key={role.key} disabled={!!downloading} onClick={() => download(role)}>
      <Download size={14} /> {downloading === role.key ? 'Downloading…' : `Download original ${role.label}`}
    </button>)}
    {error && <div className="error" role="alert">{error}</div>}
  </div>;
}
