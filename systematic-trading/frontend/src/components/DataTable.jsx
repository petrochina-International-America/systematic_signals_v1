const CROWDING_COLORS = {
  Crowded: 'var(--red)',
  Neutral: 'var(--blue)',
  Washed: 'var(--green)',
};

export default function DataTable({ columns, data, colorRules = {} }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="data-table">
        <thead>
          <tr>
            {columns.map(col => (
              <th key={col.id}>{col.name}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => (
            <tr key={i}>
              {columns.map(col => {
                const val = row[col.id];
                const colorRule = colorRules[col.id];
                const color = colorRule ? colorRule(val) : undefined;
                return (
                  <td key={col.id} style={color ? { color, fontWeight: 600 } : undefined}>
                    {val}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function positioningColorRules() {
  return {
    Crowding: (val) => CROWDING_COLORS[val] || undefined,
  };
}
