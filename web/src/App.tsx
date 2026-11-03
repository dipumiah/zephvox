import { FileText, History as HistoryIcon, House, Settings as SettingsIcon, Target, TrendingUp } from "lucide-react";
import { useEffect, useRef } from "react";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ModelDownloadsBanner } from "@/components/ModelDownloads";
import { Notices } from "@/components/Notices";
import { Sky } from "@/components/Sky";
import { useEngine } from "@/engine";
import { cn } from "@/lib/utils";
import { navigate, useRoute, type View } from "@/router";
import { History } from "@/views/History";
import { Home } from "@/views/Home";
import { Practice } from "@/views/Practice";
import { Progress } from "@/views/Progress";
import { Report } from "@/views/Report";
import { Settings } from "@/views/Settings";

const NAV: { view: View; label: string; icon: typeof House }[] = [
  { view: "home", label: "Home", icon: House },
  { view: "report", label: "Report", icon: FileText },
  { view: "practice", label: "Practice", icon: Target },
  { view: "history", label: "History", icon: HistoryIcon },
  { view: "progress", label: "Progress", icon: TrendingUp },
  { view: "status", label: "Settings", icon: SettingsIcon },
];

function Tabs({ current }: { current: View }) {
  return (
    <nav aria-label="Main" className="hidden sm:block">
      <ul className="m-0 inline-flex list-none gap-1 rounded-full bg-foreground/[0.05] p-1">
        {NAV.map(({ view, label }) => {
          const active = current === view;
          return (
            <li key={view}>
              <button
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => navigate(view)}
                className={cn(
                  "h-9 rounded-full px-4 text-sm font-semibold whitespace-nowrap transition-colors",
                  active ? "bg-card text-foreground shadow-soft" : "text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

/** Phones: the same pages as a tab bar along the bottom edge. */
function BottomTabs({ current }: { current: View }) {
  return (
    <nav
      aria-label="Main"
      className="fixed inset-x-0 bottom-0 z-30 border-t border-border bg-card/95 pb-[env(safe-area-inset-bottom)] backdrop-blur sm:hidden"
    >
      <ul className="m-0 grid list-none grid-cols-6 p-0">
        {NAV.map(({ view, label, icon: Icon }) => {
          const active = current === view;
          return (
            <li key={view}>
              <button
                type="button"
                aria-current={active ? "page" : undefined}
                onClick={() => navigate(view)}
                className={cn(
                  "grid h-15 w-full place-items-center content-center gap-0.5 text-[11px] font-semibold",
                  active ? "text-link" : "text-muted-foreground",
                )}
              >
                <Icon className="size-5" strokeWidth={active ? 2.4 : 2} />
                {label}
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

export function App() {
  const route = useRoute();
  const { state } = useEngine();
  const seen = useRef(state.lastAnalysis?.seq ?? 0);

  // A finished analysis (started from here, a shortcut or the CLI) opens its report.
  useEffect(() => {
    const a = state.lastAnalysis;
    if (a && a.seq !== seen.current) {
      seen.current = a.seq;
      if (route.view !== "practice") navigate("report", a.sessionId);
    }
  }, [state.lastAnalysis]);

  // Each page starts at the top.
  useEffect(() => {
    window.scrollTo({ top: 0 });
  }, [route.view, route.id]);

  return (
    <TooltipProvider delayDuration={300}>
      <div className="min-h-full lg:grid lg:grid-cols-[clamp(320px,30vw,400px)_minmax(0,1fr)]">
        <Sky route={route} variant="pane" className="sticky top-0 hidden h-dvh lg:flex" />
        <div className="min-w-0">
          <Sky route={route} variant="strip" className="sticky top-0 z-30 lg:hidden" />
          <div className="mx-auto w-full max-w-[920px] px-4 pt-6 pb-28 sm:px-8 sm:pb-20 lg:pt-8">
            <Tabs current={route.view} />
            <main className="sm:pt-8">
              <ModelDownloadsBanner onOpen={() => navigate("status")} />
              {route.view === "home" && <Home />}
              {route.view === "report" && <Report id={route.id} />}
              {route.view === "practice" && <Practice route={route} />}
              {route.view === "history" && <History />}
              {route.view === "progress" && <Progress />}
              {route.view === "status" && <Settings />}
            </main>
          </div>
        </div>
      </div>
      <BottomTabs current={route.view} />
      <Notices />
      <Toaster position="bottom-right" mobileOffset={{ bottom: 76 }} />
    </TooltipProvider>
  );
}
