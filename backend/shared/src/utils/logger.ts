// Structured logging utility

export enum LogLevel {
  DEBUG = 'debug',
  INFO = 'info',
  WARN = 'warn',
  ERROR = 'error',
}

interface LogEntry {
  timestamp: string;
  level: LogLevel;
  service: string;
  message: string;
  userId?: string;
  requestId?: string;
  details?: any;
}

class Logger {
  private serviceName: string;
  private minLevel: LogLevel;

  constructor(serviceName: string) {
    this.serviceName = serviceName;
    this.minLevel = this.getMinLevel();
  }

  private getMinLevel(): LogLevel {
    const level = process.env.LOG_LEVEL?.toLowerCase() || 'info';
    return level as LogLevel;
  }

  private shouldLog(level: LogLevel): boolean {
    const levels = [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARN, LogLevel.ERROR];
    const minIndex = levels.indexOf(this.minLevel);
    const currentIndex = levels.indexOf(level);
    return currentIndex >= minIndex;
  }

  private log(level: LogLevel, message: string, details?: any, userId?: string, requestId?: string) {
    if (!this.shouldLog(level)) return;

    const entry: LogEntry = {
      timestamp: new Date().toISOString(),
      level,
      service: this.serviceName,
      message,
      ...(userId && { userId }),
      ...(requestId && { requestId }),
      ...(details && { details }),
    };

    const logString = JSON.stringify(entry);

    switch (level) {
      case LogLevel.ERROR:
        console.error(logString);
        break;
      case LogLevel.WARN:
        console.warn(logString);
        break;
      default:
        console.log(logString);
    }
  }

  debug(message: string, details?: any, userId?: string, requestId?: string) {
    this.log(LogLevel.DEBUG, message, details, userId, requestId);
  }

  info(message: string, details?: any, userId?: string, requestId?: string) {
    this.log(LogLevel.INFO, message, details, userId, requestId);
  }

  warn(message: string, details?: any, userId?: string, requestId?: string) {
    this.log(LogLevel.WARN, message, details, userId, requestId);
  }

  error(message: string, details?: any, userId?: string, requestId?: string) {
    this.log(LogLevel.ERROR, message, details, userId, requestId);
  }
}

export function createLogger(serviceName: string): Logger {
  return new Logger(serviceName);
}

