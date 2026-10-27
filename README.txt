NG-ClassMate V8 — Zoom transcription + memory optimization

Zoom automatic transcription
- Handles Zoom recording.completed and recording transcript-completed webhook events.
- Polls pending recordings automatically with increasing delays until the Zoom VTT transcript is actually downloadable.
- Detects transcript VTT/TRANSCRIPT/AUDIO_TRANSCRIPT/original_transcript files.
- Automatic ingestion does NOT download the full Zoom audio as a fallback; it waits for Zoom's own transcript.
- The teacher's explicit Generate Transcript action remains available as the fallback.
- Manual transcription downloads Zoom audio as a streamed temporary file; audio is never kept as one giant in-memory response.

Memory optimization
- Recording transcripts are stored separately in DATA_DIR/transcripts rather than inside the global RECORDINGS list.
- RECORDINGS contains lightweight recording metadata only.
- Legacy recordings.json containing segments/embeddings is migrated once using a streaming JSON parser.
- Transcript embedding vectors are never persisted in recording objects.
- Transcript semantic retrieval is bounded to small candidate sets and embedding concurrency is limited to one worker.
- Chat/AI work is concurrency-limited (AI_CONCURRENCY defaults to 2).
- Uploads are read in bounded 1 MB chunks with size limits.
- Deleting a recording also deletes its transcript file.
- /api/diag/memory reports safe memory/storage diagnostics.

Syllabus Map
- Keeps the Notes-Only, contextual, teacher-material-first mapping discussed with the user.
- Official syllabus files are not used by the Notes-Only Syllabus Map flow.


V9 change: Student Syllabus Map has been removed from the UI and backend. The transcription and memory optimizations from V8 remain. Past Paper Solver and teacher notes remain available.

V10 change: Short quiz relevance and source-grounding
- If teacher notes are attached to a recording, they are the only source used to generate quiz facts. The transcript is used only to find a relevant review timestamp after generation, so side remarks in the transcript cannot become quiz topics.
- If there are no attached teacher notes, the generator uses bounded contiguous transcript windows rather than isolated evenly-spaced segments, and is instructed to ignore non-instructional remarks and tangents.
- Quiz candidates must include a supporting quote copied from the permitted source. The backend validates that quote against the source text and drops malformed, unsupported, duplicate, or ambiguous candidates instead of presenting them.
- Question options/answer indices are validated; timestamps are shown only when a relevant transcript anchor is found.
- Existing V9 Zoom transcription and memory-optimization behavior is preserved. Student Syllabus Map remains removed.
