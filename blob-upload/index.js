import express from 'express';
import { issueSignedToken, presignUrl } from '@vercel/blob';

const app = express();
const MAX_UPLOAD_BYTES = 15 * 1024 * 1024;
const allowedTypes = new Map([
  ['image/png', 'png'],
  ['image/jpeg', 'jpg'],
  ['image/tiff', 'tiff'],
  ['image/webp', 'webp'],
]);

app.use(express.json({ limit: '4kb' }));
app.post('/api/uploads/presign', async (req, res) => {
  const { object_key: pathname, content_type: contentType, size } = req.body ?? {};
  const match = /^uploads\/(\d+)\/([0-9a-f]{32})\.(png|jpg|tiff|webp)$/.exec(pathname ?? '');
  if (!match || allowedTypes.get(contentType) !== match[3]) {
    return res.status(400).json({ error: 'Invalid upload path or content type' });
  }
  if (!Number.isSafeInteger(size) || size < 1 || size > MAX_UPLOAD_BYTES) {
    return res.status(413).json({ error: 'Maximum upload size is 15 MB' });
  }

  try {
    const validUntil = Date.now() + 5 * 60 * 1000;
    const signedToken = await issueSignedToken({
      pathname,
      operations: ['put'],
      allowedContentTypes: [contentType],
      maximumSizeInBytes: size,
      validUntil,
    });
    const { presignedUrl } = await presignUrl(signedToken, {
      operation: 'put',
      pathname,
      access: 'private',
      allowedContentTypes: [contentType],
      maximumSizeInBytes: size,
      validUntil,
      allowOverwrite: false,
    });
    return res.json({ upload_url: presignedUrl });
  } catch {
    return res.status(503).json({ error: 'Could not prepare secure image upload' });
  }
});

export default app;
