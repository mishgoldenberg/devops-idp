import express from 'express';
import helmet from 'helmet';
import cors from 'cors';
import { createLogger, Conversation, ChatMessage } from '@devops-control-center/shared';
import { v4 as uuidv4 } from 'uuid';
import dotenv from 'dotenv';

dotenv.config();

const logger = createLogger('ai-chatbot-service');
const app = express();

const PORT = parseInt(process.env.PORT || '8006', 10);
const USE_MOCK = process.env.USE_MOCK_DATA !== 'false';

app.use(helmet());
app.use(cors());
app.use(express.json());

// In-memory storage for mock conversations
const conversations = new Map<string, Conversation>();

/**
 * Send message to AI
 */
app.post('/chat', async (req, res) => {
  try {
    const { message, conversationId, userId } = req.body;

    if (!message) {
      return res.status(400).json({
        success: false,
        error: 'Message required',
      });
    }

    // Mock AI response
    await new Promise(resolve => setTimeout(resolve, 500)); // Simulate processing

    const userMessage: ChatMessage = {
      id: uuidv4(),
      role: 'user',
      content: message,
      timestamp: new Date(),
    };

    const assistantMessage: ChatMessage = {
      id: uuidv4(),
      role: 'assistant',
      content: generateMockResponse(message),
      timestamp: new Date(),
    };

    // Store or update conversation
    let conversation: Conversation;
    if (conversationId && conversations.has(conversationId)) {
      conversation = conversations.get(conversationId)!;
      conversation.messages.push(userMessage, assistantMessage);
      conversation.updated_at = new Date();
    } else {
      conversation = {
        id: conversationId || uuidv4(),
        user_id: userId,
        title: message.substring(0, 50),
        messages: [userMessage, assistantMessage],
        created_at: new Date(),
        updated_at: new Date(),
      };
      conversations.set(conversation.id, conversation);
    }

    res.json({
      success: true,
      data: {
        message: assistantMessage,
        conversationId: conversation.id,
      },
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Chat error', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to process message',
    });
  }
});

/**
 * Get user conversations
 */
app.get('/conversations', async (req, res) => {
  try {
    const userId = req.query.userId as string;

    const userConversations = Array.from(conversations.values())
      .filter(conv => conv.user_id === userId)
      .sort((a, b) => b.updated_at.getTime() - a.updated_at.getTime());

    res.json({
      success: true,
      data: userConversations,
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch conversations', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch conversations',
    });
  }
});

/**
 * Get conversation by ID
 */
app.get('/conversations/:id', async (req, res) => {
  try {
    const { id } = req.params;

    if (!conversations.has(id)) {
      return res.status(404).json({
        success: false,
        error: 'Conversation not found',
      });
    }

    res.json({
      success: true,
      data: conversations.get(id),
      timestamp: new Date(),
    });
  } catch (error: any) {
    logger.error('Failed to fetch conversation', { error: error.message });
    res.status(500).json({
      success: false,
      error: 'Failed to fetch conversation',
    });
  }
});

/**
 * Health check
 */
app.get('/health', (req, res) => {
  res.json({
    status: 'healthy',
    service: 'ai-chatbot-service',
    useMock: USE_MOCK,
    timestamp: new Date(),
  });
});

function generateMockResponse(message: string): string {
  const lowerMessage = message.toLowerCase();

  if (lowerMessage.includes('how') || lowerMessage.includes('what')) {
    return 'Great question! Based on the context, I can help you with that. The DevOps Control Center provides centralized access to your development tools and workflows. Would you like me to explain a specific feature?';
  } else if (lowerMessage.includes('error') || lowerMessage.includes('issue')) {
    return 'I understand you\'re experiencing an issue. Can you provide more details about the error message or the steps that led to it? I\'ll help you troubleshoot.';
  } else if (lowerMessage.includes('deploy')) {
    return 'For deployment-related questions, I can help you understand the pipeline status, recent deployments, and best practices. You can also check the Azure DevOps widget for real-time pipeline information.';
  } else {
    return 'I\'m here to help! I can assist with questions about the DevOps Control Center, your projects, pipelines, code quality, and more. What would you like to know?';
  }
}

app.listen(PORT, () => {
  logger.info('AI Chatbot service started', { port: PORT });
});

