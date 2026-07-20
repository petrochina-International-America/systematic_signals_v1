export default function Loading({ message = 'Loading...' }) {
  return (
    <div className="placeholder-note">{message}</div>
  );
}

export function ErrorNote({ message }) {
  return (
    <div className="placeholder-note" style={{ borderColor: 'var(--red)', color: 'var(--red)' }}>
      {message}
    </div>
  );
}
