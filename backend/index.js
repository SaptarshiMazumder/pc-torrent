const express = require('express');
const cors = require('cors');
const multer = require('multer');
const path = require('path');
const fs = require('fs');
const { v4: uuidv4 } = require('uuid');
const db = require('./db');

const app = express();
const PORT = 3001;

app.use(cors());
app.use(express.json());

// --- Multer storage for blender file uploads ---
const storage = multer.diskStorage({
  destination: (req, file, cb) => {
    const jobId = req.jobId; // set before upload in route
    const dir = path.join(__dirname, 'jobs', jobId, 'input');
    fs.mkdirSync(dir, { recursive: true });
    cb(null, dir);
  },
  filename: (req, file, cb) => {
    cb(null, file.originalname);
  }
});
const upload = multer({ storage });

// -----------------------------------------------
// MACHINE ENDPOINTS
// -----------------------------------------------

// Register a machine
app.post('/machines/register', (req, res) => {
  const { gpu_model, gpu_vram_gb, cpu_cores, ram_gb } = req.body;
  if (!gpu_model || !gpu_vram_gb || !cpu_cores || !ram_gb) {
    return res.status(400).json({ error: 'Missing required fields: gpu_model, gpu_vram_gb, cpu_cores, ram_gb' });
  }
  const id = uuidv4();
  db.prepare(`
    INSERT INTO machines (id, gpu_model, gpu_vram_gb, cpu_cores, ram_gb, status, registered_at)
    VALUES (?, ?, ?, ?, ?, 'idle', ?)
  `).run(id, gpu_model, gpu_vram_gb, cpu_cores, ram_gb, new Date().toISOString());

  console.log(`[MACHINE] Registered: ${gpu_model} (${id})`);
  res.json({ machine_id: id });
});

// Mark machine as available
app.put('/machines/:id/available', (req, res) => {
  const { id } = req.params;
  const machine = db.prepare('SELECT * FROM machines WHERE id = ?').get(id);
  if (!machine) return res.status(404).json({ error: 'Machine not found' });

  db.prepare('UPDATE machines SET status = ? WHERE id = ?').run('available', id);
  console.log(`[MACHINE] Available: ${id}`);
  res.json({ success: true });
});

// Mark machine as idle (goes offline)
app.put('/machines/:id/idle', (req, res) => {
  const { id } = req.params;
  db.prepare('UPDATE machines SET status = ? WHERE id = ?').run('idle', id);
  res.json({ success: true });
});

// List all available machines
app.get('/machines', (req, res) => {
  const machines = db.prepare(`SELECT * FROM machines WHERE status = 'available' ORDER BY gpu_vram_gb DESC`).all();
  res.json(machines);
});

// -----------------------------------------------
// JOB ENDPOINTS
// -----------------------------------------------

// Submit a job (renter uploads .blend file + selects machine)
app.post('/jobs', (req, res, next) => {
  req.jobId = uuidv4(); // create job id before multer runs
  next();
}, upload.single('blender_file'), (req, res) => {
  const { machine_id } = req.body;
  const file = req.file;

  if (!machine_id || !file) {
    return res.status(400).json({ error: 'machine_id and blender_file are required' });
  }

  const machine = db.prepare(`SELECT * FROM machines WHERE id = ? AND status = 'available'`).get(machine_id);
  if (!machine) return res.status(400).json({ error: 'Machine not available' });

  const jobId = req.jobId;
  db.prepare(`
    INSERT INTO jobs (id, machine_id, input_filename, status, output_files, submitted_at)
    VALUES (?, ?, ?, 'pending', '[]', ?)
  `).run(jobId, machine_id, file.originalname, new Date().toISOString());

  // Mark machine as processing so it doesn't get double-booked
  db.prepare(`UPDATE machines SET status = 'processing' WHERE id = ?`).run(machine_id);

  console.log(`[JOB] Submitted: ${jobId} → machine ${machine_id} (${file.originalname})`);
  res.json({ job_id: jobId, status: 'pending' });
});

// Get job status (renter polls this)
app.get('/jobs/:id', (req, res) => {
  const job = db.prepare('SELECT * FROM jobs WHERE id = ?').get(req.params.id);
  if (!job) return res.status(404).json({ error: 'Job not found' });

  res.json({
    ...job,
    output_files: JSON.parse(job.output_files)
  });
});

// Agent polls for next job assigned to its machine
app.get('/jobs/next-for-machine/:machine_id', (req, res) => {
  const job = db.prepare(`
    SELECT * FROM jobs WHERE machine_id = ? AND status = 'pending' ORDER BY submitted_at ASC LIMIT 1
  `).get(req.params.machine_id);

  if (!job) return res.json(null);

  // Build the input file URL
  const inputUrl = `http://localhost:${PORT}/jobs/${job.id}/input/${job.input_filename}`;
  res.json({ ...job, input_url: inputUrl });
});

// Agent updates job status
app.put('/jobs/:id/status', (req, res) => {
  const { status, error, output_files } = req.body;
  const job = db.prepare('SELECT * FROM jobs WHERE id = ?').get(req.params.id);
  if (!job) return res.status(404).json({ error: 'Job not found' });

  const updates = { status };
  if (status === 'done' || status === 'failed') {
    updates.completed_at = new Date().toISOString();
    // Free up the machine
    db.prepare(`UPDATE machines SET status = 'available' WHERE id = ?`).run(job.machine_id);
  }
  if (error) updates.error = error;
  if (output_files) updates.output_files = JSON.stringify(output_files);

  db.prepare(`
    UPDATE jobs SET status = ?, completed_at = ?, error = ?, output_files = ? WHERE id = ?
  `).run(
    updates.status,
    updates.completed_at || null,
    updates.error || null,
    updates.output_files || job.output_files,
    req.params.id
  );

  console.log(`[JOB] Status update: ${req.params.id} → ${status}`);
  res.json({ success: true });
});

// Agent uploads output file(s)
app.post('/jobs/:id/output', (req, res, next) => {
  req.jobId = req.params.id;
  next();
}, multer({
  storage: multer.diskStorage({
    destination: (req, file, cb) => {
      const dir = path.join(__dirname, 'jobs', req.jobId, 'output');
      fs.mkdirSync(dir, { recursive: true });
      cb(null, dir);
    },
    filename: (req, file, cb) => cb(null, file.originalname)
  })
}).array('files'), (req, res) => {
  const filenames = req.files.map(f => f.originalname);
  const job = db.prepare('SELECT * FROM jobs WHERE id = ?').get(req.params.id);
  if (!job) return res.status(404).json({ error: 'Job not found' });

  const existing = JSON.parse(job.output_files);
  const merged = [...new Set([...existing, ...filenames])];
  db.prepare('UPDATE jobs SET output_files = ? WHERE id = ?').run(JSON.stringify(merged), req.params.id);

  console.log(`[JOB] Output uploaded for ${req.params.id}: ${filenames.join(', ')}`);
  res.json({ success: true, files: merged });
});

// Serve input files (for agent to download)
app.use('/jobs/:id/input', (req, res, next) => {
  req.jobPath = path.join(__dirname, 'jobs', req.params.id, 'input');
  next();
}, (req, res) => {
  const filename = req.path.replace('/', '');
  const filePath = path.join(__dirname, 'jobs', req.params.id, 'input', filename);
  if (!fs.existsSync(filePath)) return res.status(404).json({ error: 'File not found' });
  res.download(filePath);
});

// Download output (renter downloads rendered files)
app.get('/jobs/:id/download', (req, res) => {
  const job = db.prepare('SELECT * FROM jobs WHERE id = ?').get(req.params.id);
  if (!job) return res.status(404).json({ error: 'Job not found' });
  if (job.status !== 'done') return res.status(400).json({ error: 'Job not complete yet' });

  const outputDir = path.join(__dirname, 'jobs', req.params.id, 'output');
  const files = JSON.parse(job.output_files);
  if (files.length === 0) return res.status(404).json({ error: 'No output files found' });

  // If single file, send it directly
  if (files.length === 1) {
    return res.download(path.join(outputDir, files[0]));
  }

  // Multiple files: zip them
  const archiver = require('archiver');
  res.setHeader('Content-Type', 'application/zip');
  res.setHeader('Content-Disposition', `attachment; filename=job_${req.params.id}_output.zip`);
  const archive = archiver('zip');
  archive.pipe(res);
  files.forEach(f => archive.file(path.join(outputDir, f), { name: f }));
  archive.finalize();
});

// Serve output files individually
app.use('/jobs/:id/output', express.static(path.join(__dirname)));

app.get('/', (req, res) => res.json({ status: 'PC Rent backend running', port: PORT }));

app.listen(PORT, () => {
  console.log(`Backend running on http://localhost:${PORT}`);
});
