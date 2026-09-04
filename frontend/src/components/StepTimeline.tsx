interface TimelineEntry {
  label: string;
  hasError?: boolean;
}

interface Props {
  items: TimelineEntry[];
  activeIndex: number;
  onSelect: (index: number) => void;
}

export default function StepTimeline({ items, activeIndex, onSelect }: Props) {
  return (
    <div className="step-timeline">
      {items.map((item, index) => (
        <button
          key={`${item.label}-${index}`}
          type="button"
          className={
            "step-timeline-item" +
            (index === activeIndex ? " active" : "") +
            (item.hasError ? " has-error" : "")
          }
          onClick={() => onSelect(index)}
        >
          <span className="step-index">{index}</span>
          <span className="step-label">{item.label}</span>
        </button>
      ))}
    </div>
  );
}
